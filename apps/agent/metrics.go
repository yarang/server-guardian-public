package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)


var agentStartTime = time.Now()

// CollectMetrics gathers current host telemetries
func CollectMetrics() (SystemMetrics, error) {
	hostname, _ := os.Hostname()

	metrics := SystemMetrics{
		Timestamp:   time.Now(),
		Hostname:    hostname,
		OS:          runtime.GOOS,
		Arch:        runtime.GOARCH,
		UptimeSec:   getSystemUptime(),
		LoadAvg:     getLoadAvg(),
		TopProcesses: getTopProcesses(5),
	}

	// Memory & Disk
	memTotal, memUsed, memPct := getMemoryStats()
	metrics.MemoryTotal = memTotal
	metrics.MemoryUsed = memUsed
	metrics.MemoryPct = memPct

	diskTotal, diskUsed, diskPct := getDiskStats()
	metrics.DiskTotal = diskTotal
	metrics.DiskUsed = diskUsed
	metrics.DiskPct = diskPct

	cpu, err := getCPUUsage()
	metrics.CPUUsagePct = cpu

	metrics.Issues = collectionIssues(metrics, err)
	return metrics, nil
}

// collectionIssues lists the metrics this host could not read, so callers can tell a real
// zero from a missing value.
func collectionIssues(m SystemMetrics, cpuErr error) []string {
	var issues []string
	if m.UptimeSec == 0 {
		issues = append(issues, "uptime unavailable")
	}
	if cpuErr != nil {
		issues = append(issues, "cpu usage unavailable: "+cpuErr.Error())
	}
	if m.MemoryTotal == 0 {
		issues = append(issues, "memory unavailable")
	}
	if m.DiskTotal == 0 {
		issues = append(issues, "disk unavailable")
	}
	return issues
}

// getSystemUptime returns seconds since the host booted (not since the agent started), or 0
// when it cannot be read.
func getSystemUptime() uint64 {
	if runtime.GOOS == "darwin" {
		out, err := exec.Command("sysctl", "-n", "kern.boottime").Output()
		if err != nil {
			return 0
		}
		return parseBoottime(string(out), time.Now())
	}
	data, err := os.ReadFile("/proc/uptime")
	if err != nil {
		return 0
	}
	return parseProcUptime(string(data))
}

// parseProcUptime reads the first field of /proc/uptime ("148726.75 291899.71").
func parseProcUptime(s string) uint64 {
	f := strings.Fields(s)
	if len(f) == 0 {
		return 0
	}
	v, err := strconv.ParseFloat(f[0], 64)
	if err != nil || v < 0 {
		return 0
	}
	return uint64(v)
}

// parseBoottime reads macOS kern.boottime ("{ sec = 1790660989, usec = 598461 } ...").
func parseBoottime(s string, now time.Time) uint64 {
	i := strings.Index(s, "sec = ")
	if i < 0 {
		return 0
	}
	f := strings.FieldsFunc(s[i+len("sec = "):], func(r rune) bool { return r == ',' || r == ' ' })
	if len(f) == 0 {
		return 0
	}
	sec, err := strconv.ParseInt(f[0], 10, 64)
	if err != nil || sec <= 0 || sec > now.Unix() {
		return 0
	}
	return uint64(now.Unix() - sec)
}

func getLoadAvg() [3]float64 {
	var loads [3]float64
	out, err := exec.Command("uptime").Output()
	if err != nil {
		return loads
	}
	str := string(out)
	// Parse "load averages: 1.25 1.50 1.60" or "load average: 1.25, 1.50, 1.60"
	idx := strings.Index(str, "load average")
	if idx == -1 {
		return loads
	}
	part := str[idx:]
	colonIdx := strings.Index(part, ":")
	if colonIdx != -1 {
		part = part[colonIdx+1:]
	}
	parts := strings.Fields(strings.ReplaceAll(part, ",", " "))
	for i := 0; i < len(parts) && i < 3; i++ {
		if val, err := strconv.ParseFloat(parts[i], 64); err == nil {
			loads[i] = val
		}
	}
	return loads
}

// getMemoryStats returns total, used and percent used. A value it cannot read is reported
// as zero rather than estimated.
func getMemoryStats() (uint64, uint64, float64) {
	if runtime.GOOS == "darwin" {
		totalOut, err := exec.Command("sysctl", "-n", "hw.memsize").Output()
		if err != nil {
			return 0, 0, 0
		}
		total, err := strconv.ParseUint(strings.TrimSpace(string(totalOut)), 10, 64)
		if err != nil || total == 0 {
			return 0, 0, 0
		}
		vmOut, err := exec.Command("vm_stat").Output()
		if err != nil {
			return total, 0, 0
		}
		used := parseVMStat(string(vmOut))
		if used > total {
			used = total
		}
		return total, used, float64(used) / float64(total) * 100.0
	}

	// Linux /proc/meminfo
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, 0, 0
	}
	var memTotal, memAvailable uint64
	lines := strings.Split(string(data), "\n")
	for _, line := range lines {
		fields := strings.Fields(line)
		if len(fields) >= 2 {
			if fields[0] == "MemTotal:" {
				val, _ := strconv.ParseUint(fields[1], 10, 64)
				memTotal = val * 1024
			} else if fields[0] == "MemAvailable:" {
				val, _ := strconv.ParseUint(fields[1], 10, 64)
				memAvailable = val * 1024
			}
		}
	}
	if memTotal > 0 {
		used := memTotal - memAvailable
		pct := float64(used) / float64(memTotal) * 100.0
		return memTotal, used, pct
	}
	return 0, 0, 0
}

// parseVMStat computes used memory the way Activity Monitor's "Memory Used" does: app memory
// (active pages) + wired + compressed, in bytes. The page size comes from vm_stat's header.
func parseVMStat(out string) uint64 {
	pageSize := uint64(4096)
	pages := map[string]uint64{}
	for _, line := range strings.Split(out, "\n") {
		if i := strings.Index(line, "page size of "); i >= 0 {
			if f := strings.Fields(line[i+len("page size of "):]); len(f) > 0 {
				if v, err := strconv.ParseUint(f[0], 10, 64); err == nil && v > 0 {
					pageSize = v
				}
			}
			continue
		}
		name, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		if v, err := strconv.ParseUint(strings.TrimSuffix(strings.TrimSpace(value), "."), 10, 64); err == nil {
			pages[strings.TrimSpace(name)] = v
		}
	}
	return (pages["Pages active"] + pages["Pages wired down"] + pages["Pages occupied by compressor"]) * pageSize
}

func getDiskStats() (uint64, uint64, float64) {
	out, err := exec.Command("df", "-k", "/").Output()
	if err != nil {
		return 0, 0, 0
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) < 2 {
		return 0, 0, 0
	}
	fields := strings.Fields(lines[len(lines)-1])
	if len(fields) >= 5 {
		totalKb, _ := strconv.ParseUint(fields[1], 10, 64)
		usedKb, _ := strconv.ParseUint(fields[2], 10, 64)
		pctStr := strings.TrimSuffix(fields[4], "%")
		pct, _ := strconv.ParseFloat(pctStr, 64)
		return totalKb * 1024, usedKb * 1024, pct
	}
	return 0, 0, 0
}

// cpuSampler measures CPU busy time between two readings. Linux diffs /proc/stat against the
// previous call (or a short fresh interval when there is none); macOS asks top for a one-second
// sample and caches it briefly, since each sample costs a second.
var cpuSampler = struct {
	sync.Mutex
	prev     cpuTimes
	prevAt   time.Time
	darwin   float64
	darwinAt time.Time
}{}

type cpuTimes struct{ busy, total uint64 }

func getCPUUsage() (float64, error) {
	if runtime.GOOS == "darwin" {
		return darwinCPUUsage()
	}
	cpuSampler.Lock()
	defer cpuSampler.Unlock()
	cur, err := readProcStat()
	if err != nil {
		return 0, err
	}
	prev := cpuSampler.prev
	if cpuSampler.prevAt.IsZero() || time.Since(cpuSampler.prevAt) > time.Minute {
		prev = cur
		time.Sleep(250 * time.Millisecond)
		if cur, err = readProcStat(); err != nil {
			return 0, err
		}
	}
	cpuSampler.prev, cpuSampler.prevAt = cur, time.Now()
	return cpuPercent(prev, cur), nil
}

func cpuPercent(prev, cur cpuTimes) float64 {
	if cur.total <= prev.total || cur.busy < prev.busy {
		return 0
	}
	pct := float64(cur.busy-prev.busy) / float64(cur.total-prev.total) * 100
	if pct > 100 {
		pct = 100
	}
	return pct
}

func readProcStat() (cpuTimes, error) {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return cpuTimes{}, err
	}
	return parseProcStat(string(data))
}

// parseProcStat reads the aggregate "cpu" line: user nice system idle iowait irq softirq steal.
// Idle and iowait count as idle; guest time is already included in user.
func parseProcStat(s string) (cpuTimes, error) {
	for _, line := range strings.Split(s, "\n") {
		f := strings.Fields(line)
		if len(f) < 5 || f[0] != "cpu" {
			continue
		}
		var t cpuTimes
		for i, v := range f[1:] {
			if i >= 8 {
				break
			}
			n, err := strconv.ParseUint(v, 10, 64)
			if err != nil {
				return cpuTimes{}, fmt.Errorf("bad /proc/stat value %q", v)
			}
			t.total += n
			if i != 3 && i != 4 {
				t.busy += n
			}
		}
		return t, nil
	}
	return cpuTimes{}, fmt.Errorf("no cpu line in /proc/stat")
}

func darwinCPUUsage() (float64, error) {
	cpuSampler.Lock()
	defer cpuSampler.Unlock()
	if time.Since(cpuSampler.darwinAt) < 3*time.Second {
		return cpuSampler.darwin, nil
	}
	out, err := exec.Command("top", "-l", "2", "-n", "0", "-s", "1").Output()
	if err != nil {
		return 0, err
	}
	pct, err := parseTopCPU(string(out))
	if err != nil {
		return 0, err
	}
	cpuSampler.darwin, cpuSampler.darwinAt = pct, time.Now()
	return pct, nil
}

// parseTopCPU takes the last "CPU usage: … idle" line; top's first sample covers time since boot.
func parseTopCPU(s string) (float64, error) {
	idle := -1.0
	for _, line := range strings.Split(s, "\n") {
		if !strings.HasPrefix(strings.TrimSpace(line), "CPU usage:") {
			continue
		}
		for _, part := range strings.Split(line, ",") {
			if f := strings.Fields(part); len(f) >= 2 && f[len(f)-1] == "idle" {
				if v, err := strconv.ParseFloat(strings.TrimSuffix(f[len(f)-2], "%"), 64); err == nil {
					idle = v
				}
			}
		}
	}
	if idle < 0 {
		return 0, fmt.Errorf("no CPU usage line from top")
	}
	return 100 - idle, nil
}

func getTopProcesses(limit int) []ProcessInfo {
	var procs []ProcessInfo
	// ps -A -o pid,%cpu,%mem,user,comm -r (macOS) or ps aux --sort=-%cpu (Linux)
	var cmd *exec.Cmd
	if runtime.GOOS == "darwin" {
		cmd = exec.Command("ps", "-A", "-o", "pid,%cpu,%mem,user,comm", "-r")
	} else {
		cmd = exec.Command("ps", "-eo", "pid,%cpu,%mem,user,comm", "--sort=-%cpu")
	}

	out, err := cmd.Output()
	if err != nil {
		return procs
	}

	lines := strings.Split(string(out), "\n")
	count := 0
	for i := 1; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) >= 5 {
			pid, _ := strconv.Atoi(fields[0])
			cpu, _ := strconv.ParseFloat(fields[1], 64)
			mem, _ := strconv.ParseFloat(fields[2], 64)
			user := fields[3]
			comm := strings.Join(fields[4:], " ")

			procs = append(procs, ProcessInfo{
				PID:     pid,
				CPU:     cpu,
				Memory:  mem,
				User:    user,
				Command: comm,
			})
			count++
			if count >= limit {
				break
			}
		}
	}
	return procs
}

func formatBytes(b uint64) string {
	const unit = 1024
	if b < unit {
		return fmt.Sprintf("%d B", b)
	}
	div, exp := int64(unit), 0
	for n := b / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %cB", float64(b)/float64(div), "KMGTPE"[exp])
}

func findProjectRoot() string {
	cwd, err := os.Getwd()
	if err != nil {
		return "."
	}
	curr := cwd
	for {
		if _, err := os.Stat(filepath.Join(curr, "configs", "servers.json")); err == nil {
			return curr
		}
		if _, err := os.Stat(filepath.Join(curr, ".git")); err == nil {
			return curr
		}
		parent := filepath.Dir(curr)
		if parent == curr {
			break
		}
		curr = parent
	}
	return cwd
}

// @MX:WARN: [AUTO] Runs an arbitrary shell command for /api/exec.
// @MX:REASON: The shell gets its own process group so the timeout kills every child it spawned,
// not just sh; WaitDelay stops a surviving grandchild holding stdout open from blocking the reply.
func executeRaw(command string, timeoutSec int) (int, string, string, int64) {
	if timeoutSec <= 0 {
		timeoutSec = 10
	}
	start := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "sh", "-c", command)
	cmd.Dir = findProjectRoot()
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = 2 * time.Second
	stdout, stderr := newCappedBuffer(maxCommandOutput), newCappedBuffer(maxCommandOutput)
	cmd.Stdout = stdout
	cmd.Stderr = stderr

	err := cmd.Run()
	duration := time.Since(start).Milliseconds()

	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = 1
		}
	}

	return exitCode, stdout.String(), stderr.String(), duration
}


// Thresholds at which /api/status reports a host as degraded.
const (
	degradedDiskPct   = 90.0
	degradedMemoryPct = 95.0
)

// nodeHealth is "healthy" unless a metric could not be read or disk or memory is nearly full;
// reasons says which.
func nodeHealth(m SystemMetrics) (string, []string) {
	reasons := append([]string(nil), m.Issues...)
	if m.DiskTotal > 0 && m.DiskPct >= degradedDiskPct {
		reasons = append(reasons, fmt.Sprintf("disk %.0f%% full", m.DiskPct))
	}
	if m.MemoryTotal > 0 && m.MemoryPct >= degradedMemoryPct {
		reasons = append(reasons, fmt.Sprintf("memory %.0f%% used", m.MemoryPct))
	}
	if len(reasons) > 0 {
		return "degraded", reasons
	}
	return "healthy", nil
}
