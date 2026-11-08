// A tiny URL shortener service: enough net/http and encoding/json to make
// `go build` compile a realistic slice of the standard library.
package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"log"
	"net/http"
	"sync"
)

type Store struct {
	mu   sync.RWMutex
	urls map[string]string
}

func NewStore() *Store { return &Store{urls: map[string]string{}} }

func (s *Store) Shorten(url string) string {
	sum := sha256.Sum256([]byte(url))
	code := base64.RawURLEncoding.EncodeToString(sum[:])[:8]
	s.mu.Lock()
	s.urls[code] = url
	s.mu.Unlock()
	return code
}

func (s *Store) Lookup(code string) (string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	u, ok := s.urls[code]
	return u, ok
}

func (s *Store) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /shorten", func(w http.ResponseWriter, r *http.Request) {
		var req struct{ URL string `json:"url"` }
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.URL == "" {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"code": s.Shorten(req.URL)})
	})
	mux.HandleFunc("GET /{code}", func(w http.ResponseWriter, r *http.Request) {
		u, ok := s.Lookup(r.PathValue("code"))
		if !ok {
			http.NotFound(w, r)
			return
		}
		http.Redirect(w, r, u, http.StatusFound)
	})
	return mux
}

func main() {
	log.Fatal(http.ListenAndServe(":8080", NewStore().Handler()))
}
