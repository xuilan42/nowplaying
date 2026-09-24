// nowplaying-server — приёмник Now Playing -> био Telegram.
// Один статический бинарник для OpenWrt. Last-Write-Wins, без очередей.
package main

import (
	"bufio"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf16"

	"github.com/gotd/log/logslog"
	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
	"github.com/gotd/td/telegram/dcs"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
	"golang.org/x/net/proxy"
)

type Config struct {
	Listen  string `json:"listen"`
	Token   string `json:"token"`
	APIID   int    `json:"api_id"`
	APIHash string `json:"api_hash"`
	Phone   string `json:"phone"`
	Session string `json:"session"`
	Proxy   string `json:"proxy"` // socks5 host:port; пусто — напрямую

	StaleAfter time.Duration // тишина от клиента -> био очищается
	MinAPIGap  time.Duration // мин. пауза между запросами к Telegram
	CheckEvery time.Duration // период проверки состояния
	Resync     time.Duration // период сверки с реальным био профиля
	MaxBioLen  int           // 70 (140 при Premium)
}

func defaultConfig() Config {
	return Config{
		Listen: "0.0.0.0:7854", Session: "/etc/nowplaying/session.json",
		StaleAfter: 15 * time.Second, MinAPIGap: 12 * time.Second,
		CheckEvery: 2 * time.Second, Resync: time.Hour, MaxBioLen: 70,
	}
}

// ------------------------- состояние (LWW) -------------------------

type playerState struct {
	Title     string  `json:"title"`
	Artist    string  `json:"artist"`
	IsPlaying bool    `json:"is_playing"`
	Timestamp float64 `json:"timestamp"`
	recvAt    time.Time
}

type store struct {
	mu    sync.Mutex
	state *playerState // единственное "текущее" состояние; очередей нет
}

// put — Last-Write-Wins: каждое валидное сообщение затирает предыдущее.
func (s *store) put(p playerState) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state != nil && p.Timestamp < s.state.Timestamp-5 {
		return false // запаздывающий пакет, игнорируем
	}
	s.state = &p
	return true
}

// desiredBio — какой текст должен стоять в био прямо сейчас.
// Пауза, пустые данные или тишина клиента дольше stale -> "".
func (s *store) desiredBio(stale time.Duration, maxLen int) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.state
	if p == nil || !p.IsPlaying || (p.Title == "" && p.Artist == "") {
		return ""
	}
	if time.Since(p.recvAt) > stale {
		return "" // клиент отключился / вышел из Wi-Fi
	}
	return clip(formatBio(p.Artist, p.Title), maxLen)
}

func formatBio(artist, title string) string {
	switch {
	case artist != "" && title != "":
		return "🎧 " + artist + " — " + title
	case artist != "":
		return "🎧 " + artist
	default:
		return "🎧 " + title
	}
}

func utf16Len(s string) int { return len(utf16.Encode([]rune(s))) }

// clip обрезает по лимиту Telegram (UTF-16 кодовые единицы), добавляя "…".
func clip(s string, max int) string {
	if max <= 1 || utf16Len(s) <= max {
		return s
	}
	var b strings.Builder
	n := 0
	for _, r := range s {
		l := utf16.RuneLen(r)
		if l < 1 {
			l = 1
		}
		if n+l > max-1 {
			break
		}
		b.WriteRune(r)
		n += l
	}
	b.WriteRune('…')
	return b.String()
}

// ------------------------------ HTTP -------------------------------

type updatePayload struct {
	Token     string  `json:"token"`
	Title     string  `json:"title"`
	Artist    string  `json:"artist"`
	IsPlaying bool    `json:"is_playing"`
	Timestamp float64 `json:"timestamp"`
}

func handleUpdate(w http.ResponseWriter, r *http.Request, cfg Config, st *store) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	var p updatePayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if subtle.ConstantTimeCompare([]byte(p.Token), []byte(cfg.Token)) != 1 {
		http.Error(w, "bad token", http.StatusForbidden)
		return
	}
	if p.Timestamp <= 0 {
		p.Timestamp = float64(time.Now().Unix())
	}
	ok := st.put(playerState{
		Title:     trunc(strings.TrimSpace(p.Title), 120),
		Artist:    trunc(strings.TrimSpace(p.Artist), 120),
		IsPlaying: p.IsPlaying, Timestamp: p.Timestamp, recvAt: time.Now(),
	})
	detail := "stored"
	if !ok {
		detail = "stale ignored"
	}
	writeJSON(w, map[string]string{"status": "ok", "detail": detail})
}

func handleStatus(w http.ResponseWriter, _ *http.Request, st *store) {
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.state == nil {
		writeJSON(w, map[string]string{"status": "empty"})
		return
	}
	p := *st.state
	writeJSON(w, map[string]any{
		"status": "ok", "age_sec": time.Since(p.recvAt).Seconds(), "state": p,
	})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func trunc(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// --------------------------- bio updater ---------------------------

func bioUpdater(ctx context.Context, api *tg.Client, st *store, cfg *Config) error {
	var (
		current  string    // что, по нашим данным, стоит в био сейчас
		lastCall time.Time // момент последнего запроса к Telegram API
		lastSync time.Time // последняя сверка с реальным профилем
	)

	// Сверка с реальным био (первичная + периодическая):
	// закрывает ручные правки профиля и расхождения после сбоев.
	syncReal := func() {
		full, err := api.UsersGetFullUser(ctx, &tg.InputUserSelf{})
		if err != nil {
			if d, ok := tgerr.AsFloodWait(err); ok {
				log.Printf("FloodWait %v при сверке — ждём", d)
				time.Sleep(d + time.Second)
			} else {
				log.Printf("сверка био не удалась: %v", err)
			}
			return
		}
		about := full.FullUser.About
		if about != current {
			log.Printf("сверка: в профиле сейчас %q", about)
			current = about
		}
		lastSync = time.Now()
	}

	tick := time.NewTicker(cfg.CheckEvery)
	defer tick.Stop()

	for {
		if lastSync.IsZero() || time.Since(lastSync) >= cfg.Resync {
			syncReal()
		}

		desired := st.desiredBio(cfg.StaleAfter, cfg.MaxBioLen)
		switch {
		case desired == current:
			// текст не изменился — к API вообще не обращаемся
		case time.Since(lastCall) < cfg.MinAPIGap:
			// троттлинг: подождём следующего тика (возьмём САМОЕ свежее состояние)
		default:
			req := &tg.AccountUpdateProfileRequest{About: desired}
			// about = бит 2; SetFlags() нельзя — пропустил бы пустое About,
			// а очистка био и есть пустая строка.
			req.Flags.Set(2)
			_, err := api.AccountUpdateProfile(ctx, req)
			if err != nil {
				if d, ok := tgerr.AsFloodWait(err); ok {
					log.Printf("FloodWait %v — пауза", d)
					lastCall = time.Now()
					time.Sleep(d + time.Second)
				} else {
					log.Printf("UpdateProfile: %v", err)
				}
				continue
			}
			current = desired
			lastCall = time.Now()
			if desired == "" {
				log.Println("био очищено")
			} else {
				log.Printf("био обновлено: %q", desired)
			}
		}

		select {
		case <-tick.C:
		case <-ctx.Done():
			return nil
		}
	}
}

// ------------------------- первый вход (login) ---------------------

var stdinScanner = bufio.NewScanner(os.Stdin)

func readLine() (string, error) {
	if !stdinScanner.Scan() {
		if err := stdinScanner.Err(); err != nil {
			return "", err
		}
		return "", errors.New("stdin закрыт")
	}
	return strings.TrimSpace(stdinScanner.Text()), nil
}

type terminalLogin struct{ phone string }

func (t terminalLogin) Phone(_ context.Context) (string, error) {
	if t.phone != "" {
		return t.phone, nil
	}
	fmt.Print("Номер телефона (напр. +79...): ")
	return readLine()
}

func (terminalLogin) Code(_ context.Context, _ *tg.AuthSentCode) (string, error) {
	fmt.Print("Код из Telegram: ")
	return readLine()
}

func (terminalLogin) Password(_ context.Context) (string, error) {
	fmt.Print("Пароль 2FA: ")
	return readLine()
}

func (terminalLogin) AcceptTermsOfService(_ context.Context, _ tg.HelpTermsOfService) error {
	return nil
}

func (terminalLogin) SignUp(_ context.Context) (auth.UserInfo, error) {
	return auth.UserInfo{}, errors.New("регистрация новых аккаунтов не поддерживается")
}

// ------------------------------ main -------------------------------

func loadConfigFile(path string, cfg *Config) error {
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	return json.Unmarshal(data, cfg)
}

func main() {
	cfg := defaultConfig()

	fConfig := flag.String("config", "/etc/nowplaying/config.json", "путь к JSON-конфигу")
	fListen := flag.String("listen", "", "host:port HTTP")
	fToken := flag.String("token", "", "общий секрет клиента")
	fAPIID := flag.Int("api-id", 0, "api_id с my.telegram.org")
	fAPIHash := flag.String("api-hash", "", "api_hash")
	fPhone := flag.String("phone", "", "телефон для первого входа")
	fSession := flag.String("session", "", "файл сессии")
	fProxy := flag.String("proxy", "", "socks5 host:port для MTProto (пусто — напрямую)")
	fStale := flag.Duration("stale-after", 0, "")
	fGap := flag.Duration("min-api-gap", 0, "")
	fCheck := flag.Duration("check-every", 0, "")
	fResync := flag.Duration("resync", 0, "")
	fMax := flag.Int("max-bio", 0, "70 или 140 (Premium)")
	flag.Parse()

	if err := loadConfigFile(*fConfig, &cfg); err != nil {
		log.Fatalf("config: %v", err)
	}
	if v := os.Getenv("NPLAY_TOKEN"); v != "" {
		cfg.Token = v
	}
	if v := os.Getenv("NPLAY_API_ID"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.APIID = n
		}
	}
	if v := os.Getenv("NPLAY_API_HASH"); v != "" {
		cfg.APIHash = v
	}
	if v := os.Getenv("NPLAY_PHONE"); v != "" {
		cfg.Phone = v
	}
	if *fListen != "" {
		cfg.Listen = *fListen
	}
	if *fToken != "" {
		cfg.Token = *fToken
	}
	if *fAPIID != 0 {
		cfg.APIID = *fAPIID
	}
	if *fAPIHash != "" {
		cfg.APIHash = *fAPIHash
	}
	if *fPhone != "" {
		cfg.Phone = *fPhone
	}
	if *fSession != "" {
		cfg.Session = *fSession
	}
	if *fProxy != "" {
		cfg.Proxy = *fProxy
	}
	if *fStale > 0 {
		cfg.StaleAfter = *fStale
	}
	if *fGap > 0 {
		cfg.MinAPIGap = *fGap
	}
	if *fCheck > 0 {
		cfg.CheckEvery = *fCheck
	}
	if *fResync > 0 {
		cfg.Resync = *fResync
	}
	if *fMax > 0 {
		cfg.MaxBioLen = *fMax
	}

	if cfg.Token == "" || cfg.APIID == 0 || cfg.APIHash == "" {
		fmt.Fprintln(os.Stderr, "нужны token, api_id, api_hash — в конфиге или флагами/env")
		os.Exit(1)
	}

	// Ограничение памяти: если юзер задал GOMEMLIMIT/GOGC — не трогаем.
	if os.Getenv("GOMEMLIMIT") == "" {
		debug.SetMemoryLimit(48 << 20) // soft limit 48 MiB
	}
	if os.Getenv("GOGC") == "" {
		debug.SetGCPercent(60)
	}

	if dir := filepath.Dir(cfg.Session); dir != "" {
		_ = os.MkdirAll(dir, 0o700)
	}

	st := &store{}
	mux := http.NewServeMux()
	mux.HandleFunc("/update", func(w http.ResponseWriter, r *http.Request) {
		handleUpdate(w, r, cfg, st)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		handleStatus(w, r, st)
	})
	srv := &http.Server{Addr: cfg.Listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		log.Printf("HTTP на %s: POST /update, GET / — статус", cfg.Listen)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("http: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	opts := telegram.Options{
		SessionStorage: &session.FileStorage{Path: cfg.Session},
		NoUpdates:      true, // апдейты не нужны — меньше RAM и CPU
	}
	if os.Getenv("NPLAY_DEBUG") != "" {
		// Подробный лог gotd: видно, на каком шаге соединения висим.
		h := slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})
		opts.Logger = logslog.New(slog.New(h))
		log.Println("NPLAY_DEBUG: подробный лог gotd включён")
	}
	if cfg.Proxy != "" {
		d, err := proxy.SOCKS5("tcp", cfg.Proxy, nil, proxy.Direct)
		if err != nil {
			log.Fatalf("socks5 %s: %v", cfg.Proxy, err)
		}
		cd, ok := d.(proxy.ContextDialer)
		if !ok {
			log.Fatalf("socks5 %s: dialer без DialContext", cfg.Proxy)
		}
		// Весь MTProto (все DC, включая медиа/CDN) — через прокси.
		// Таймаут 20с на дозвон: зависшие соксы не должны блокировать цикл
		// переподключения навсегда.
		opts.Resolver = dcs.Plain(dcs.PlainOptions{Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
			dctx, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()
			log.Printf("dial %s через %s", addr, cfg.Proxy)
			c, err := cd.DialContext(dctx, network, addr)
			if err != nil {
				log.Printf("dial %s: %v", addr, err)
			}
			return c, err
		}})
		log.Printf("MTProto через SOCKS5 %s", cfg.Proxy)
	}

	client := telegram.NewClient(cfg.APIID, cfg.APIHash, opts)

	err := client.Run(ctx, func(ctx context.Context) error {
		status, err := client.Auth().Status(ctx)
		if err != nil {
			return fmt.Errorf("auth status: %w", err)
		}
		if !status.Authorized {
			log.Printf("Сессия не найдена — вход (телефон %s)", cfg.Phone)
			flow := auth.NewFlow(terminalLogin{phone: cfg.Phone}, auth.SendCodeOptions{})
			if err := client.Auth().IfNecessary(ctx, flow); err != nil {
				return fmt.Errorf("login: %w", err)
			}
			log.Println("Вход выполнен, сессия сохранена в", cfg.Session)
		} else {
			u := status.User
			log.Printf("Сессия активна: %s", strings.TrimSpace(u.FirstName+" "+u.LastName))
		}
		return bioUpdater(ctx, client.API(), st, &cfg)
	})

	shCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = srv.Shutdown(shCtx)

	if err != nil && !errors.Is(err, context.Canceled) {
		log.Printf("exit: %v", err)
		os.Exit(1)
	}
}
