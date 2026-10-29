package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseVMStat(t *testing.T) {
	out := `Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     6931.
Pages active:                                 100.
Pages inactive:                               270301.
Pages wired down:                             20.
"Translation faults":                     1605081551.
Pages occupied by compressor:                  3.
`
	if got, want := parseVMStat(out), uint64(123*16384); got != want {
		t.Fatalf("used = %d, want %d", got, want)
	}
	if got := parseVMStat("garbage"); got != 0 {
		t.Fatalf("unparseable output gave %d", got)
	}
}

func TestParseUptime(t *testing.T) {
	if got := parseProcUptime("148726.75 291899.71\n"); got != 148726 {
		t.Fatalf("proc uptime = %d", got)
	}
	now := time.Unix(1790661989, 0)
	if got := parseBoottime("{ sec = 1790660989, usec = 598461 } Tue Sep 29 14:49:49 2026\n", now); got != 1000 {
		t.Fatalf("boottime uptime = %d", got)
	}
	for _, bad := range []string{"", "garbage", "{ sec = 9999999999, usec = 0 }"} {
		if parseBoottime(bad, now) != 0 || parseProcUptime(bad) != 0 {
			t.Errorf("%q should give 0", bad)
		}
	}
}

func TestCPUFromProcStat(t *testing.T) {
	a, err := parseProcStat("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := parseProcStat("cpu  150 0 150 850 150 0 0 0 0 0\n")
	// busy +100 of total +300 (idle and iowait are idle time)
	if got := cpuPercent(a, b); got < 33.3 || got > 33.4 {
		t.Fatalf("cpu = %.2f, want 33.3", got)
	}
	if got := cpuPercent(b, a); got != 0 {
		t.Fatalf("counter reset gave %.2f", got)
	}
	if _, err := parseProcStat("intr 1 2 3"); err == nil {
		t.Fatal("missing cpu line accepted")
	}
}

func TestParseTopCPU(t *testing.T) {
	out := "Processes: 1\nCPU usage: 15.93% user, 11.64% sys, 72.42% idle \nx\nCPU usage: 17.7% user, 8.37% sys, 74.54% idle \n"
	got, err := parseTopCPU(out)
	if err != nil || got < 25.45 || got > 25.47 {
		t.Fatalf("cpu = %.2f err %v, want the second sample (25.46)", got, err)
	}
	if _, err := parseTopCPU("nothing"); err == nil {
		t.Fatal("missing line accepted")
	}
}

func TestNodeHealth(t *testing.T) {
	ok := SystemMetrics{UptimeSec: 10, MemoryTotal: 1, MemoryPct: 50, DiskTotal: 1, DiskPct: 50}
	if s, r := nodeHealth(ok); s != "healthy" || r != nil {
		t.Fatalf("%s %v", s, r)
	}
	full := ok
	full.DiskPct = 93
	if s, r := nodeHealth(full); s != "degraded" || len(r) != 1 {
		t.Fatalf("full disk: %s %v", s, r)
	}
	missing := ok
	missing.Issues = []string{"memory unavailable"}
	if s, _ := nodeHealth(missing); s != "degraded" {
		t.Fatal("unreadable metric reported healthy")
	}
}

// A timed-out command must take its children down with it, and the reply must not wait for them.
func TestExecuteRawTimeoutKillsChildren(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "survivor")
	start := time.Now()
	code, _, _, _ := executeRaw("(sleep 3; touch "+marker+") & sleep 30", 1)
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("executeRaw returned after %v, want about 1s", elapsed)
	}
	if code == 0 {
		t.Fatalf("exit code = 0, want non-zero for a timed-out command")
	}
	time.Sleep(3 * time.Second)
	if _, err := os.Stat(marker); err == nil {
		t.Fatalf("background child survived the timeout and wrote %s", marker)
	}
}
