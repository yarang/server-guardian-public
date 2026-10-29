package main

import (
	"reflect"
	"testing"
)

func TestListenHosts(t *testing.T) {
	cases := []struct {
		raw  string
		want []string
	}{
		{"", []string{"127.0.0.1"}},
		{"  ", []string{"127.0.0.1"}},
		{"100.64.0.7", []string{"100.64.0.7"}},
		{"127.0.0.1, 100.64.0.7,", []string{"127.0.0.1", "100.64.0.7"}},
	}
	for _, c := range cases {
		if got := listenHosts(c.raw); !reflect.DeepEqual(got, c.want) {
			t.Errorf("listenHosts(%q) = %v, want %v", c.raw, got, c.want)
		}
	}
}
