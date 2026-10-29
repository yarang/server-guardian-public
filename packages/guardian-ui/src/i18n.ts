/**
 * Built-in visible and accessible texts of the components. English is the default; an application
 * localizes the whole library once at startup with {@link setUiMessages}. A text passed as a prop to a
 * component always wins over these defaults.
 */
export interface UiMessages {
  copy: string;
  copied: string;
  navPrimary: string;
  segmentedOptions: string;
  meterUsage: string;
  verdictChain: string;
  actionAria: (id: string) => string;
  untitledAction: string;
  approve: string;
  reject: string;
  riskLabel: (risk: string) => string;
  confirmDialog: {
    title: string;
    confirm: string;
    cancel: string;
    typeToConfirm: (text: string) => string;
  };
  heatmap: {
    rowLabel: string;
    scoreLabel: string;
    legend: string;
    check: (n: number) => string;
    /** Cell word per state: P pass, W warn, F fail, I info, N not checked. */
    words: { P: string; W: string; F: string; I: string; N: string };
  };
  nodeTable: {
    label: string;
    node: string;
    status: string;
    disk: string;
    load: string;
    unavailable: (metric: string) => string;
    sshScore: (score: number) => string;
    online: string;
    offline: string;
  };
  processTable: { user: string; command: string };
  terminal: { sessions: string; output: string; command: string; placeholder: string };
  commandPalette: { label: string; search: string; scope: string; results: string; noResults: string; placeholder: string };
}

export const DEFAULT_UI_MESSAGES: UiMessages = {
  copy: 'Copy',
  copied: 'Copied',
  navPrimary: 'Primary',
  segmentedOptions: 'Options',
  meterUsage: 'Usage',
  verdictChain: 'Verdict chain',
  actionAria: (id) => `Action ${id}`,
  untitledAction: 'Untitled action',
  approve: 'Approve',
  reject: 'Reject',
  riskLabel: (risk) => `${risk} RISK`,
  confirmDialog: {
    title: 'Confirm action',
    confirm: 'Confirm',
    cancel: 'Cancel',
    typeToConfirm: (text) => `Type ${text} to confirm`,
  },
  heatmap: {
    rowLabel: 'Node',
    scoreLabel: 'Score',
    legend: 'Legend',
    check: (n) => `Check ${n}`,
    words: { P: 'Pass', W: 'Warn', F: 'Fail', I: 'Info', N: 'Not checked' },
  },
  nodeTable: {
    label: 'Fleet nodes',
    node: 'Node',
    status: 'Status',
    disk: 'Disk',
    load: 'Load',
    unavailable: (metric) => `${metric} unavailable`,
    sshScore: (score) => `SSH score ${score}`,
    online: 'online',
    offline: 'offline',
  },
  processTable: { user: 'User', command: 'Command' },
  terminal: { sessions: 'Terminal sessions', output: 'Terminal output', command: 'Command', placeholder: 'Run a command' },
  commandPalette: {
    label: 'Command palette',
    search: 'Search commands',
    scope: 'Search scope',
    results: 'Results',
    noResults: 'No results',
    placeholder: 'Search nodes, actions, debates',
  },
};

let current: UiMessages = DEFAULT_UI_MESSAGES;

/** Replaces the library texts (sections are merged over the English defaults). Call once before rendering. */
export function setUiMessages(overrides: DeepPartial<UiMessages>): void {
  const merged: Record<string, unknown> = { ...DEFAULT_UI_MESSAGES };
  for (const [key, value] of Object.entries(overrides)) {
    const base = (DEFAULT_UI_MESSAGES as unknown as Record<string, unknown>)[key];
    merged[key] =
      value && typeof value === 'object' && base && typeof base === 'object' ? { ...(base as object), ...(value as object) } : (value ?? base);
  }
  current = merged as unknown as UiMessages;
}

/** The active library texts. */
export function getUiMessages(): UiMessages {
  return current;
}

export type DeepPartial<T> = { [K in keyof T]?: T[K] extends (...args: never[]) => unknown ? T[K] : T[K] extends object ? Partial<T[K]> : T[K] };
