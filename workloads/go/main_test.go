package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestShortenAndRedirect(t *testing.T) {
	srv := httptest.NewServer(NewStore().Handler())
	defer srv.Close()
	resp, err := http.Post(srv.URL+"/shorten", "application/json", strings.NewReader(`{"url":"https://example.com"}`))
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("shorten: %v %v", err, resp)
	}
}

func TestLookupMissing(t *testing.T) {
	if _, ok := NewStore().Lookup("nope"); ok {
		t.Fatal("found a code that was never stored")
	}
}
