package main

import (
	"strings"
	"testing"
)

func TestCappedBufferKeepsTheHeadAndCountsTheRest(t *testing.T) {
	b := newCappedBuffer(10)
	for i := 0; i < 3; i++ {
		n, err := b.Write([]byte("abcdef"))
		if n != 6 || err != nil {
			t.Fatalf("Write = %d, %v; a capped buffer must accept everything", n, err)
		}
	}
	got := b.String()
	if !strings.HasPrefix(got, "abcdefabcd\n[output truncated: 8 more bytes") {
		t.Fatalf("unexpected output %q", got)
	}
}

func TestCappedBufferBelowTheLimitIsPlain(t *testing.T) {
	b := newCappedBuffer(100)
	_, _ = b.Write([]byte("hello"))
	if b.String() != "hello" {
		t.Fatalf("got %q", b.String())
	}
}
