// English copy: app frame, navigation, hub connection / sign-in / settings dialogs, shared components, API error text.
// Rules and glossary: see ../ko.ts.
import type { Messages } from '../types';

export const shell: Messages['shell'] = {
  brand: { name: 'Server Guardian', sub: 'Web dashboard' },
  nav: {
    primary: 'Main menu',
    primaryMobile: 'Main menu (mobile)',
    routes: {
      overview: { label: 'Overview', title: 'Overview' },
      fleet: { label: 'Nodes', title: 'Nodes' },
      debates: { label: 'Workflows', title: 'Workflows' },
      approvals: { label: 'Approvals', title: 'Approvals' },
      settings: { label: 'Settings', title: 'Settings' },
    },
    fleetTabs: {
      nodes: 'Nodes',
      groups: 'Groups',
      ssh: 'SSH audit',
      commands: 'Commands',
      commandSets: 'Command sets',
    },
  },
  header: {
    hubConnection: 'Hub connection',
    refresh: 'Refresh',
    hubLabel: 'Hub',
  },
  status: {
    checking: 'Checking hub…',
    signInRequired: 'Sign-in required',
    online: 'Hub online',
    offline: 'Hub offline',
    degraded: 'Hub degraded',
    degradedAria: (why: string) => `Hub degraded: ${why}`,
  },
  documentTitle: 'Server Guardian - Intelligent SSH and node fleet management',

  login: {
    title: 'Sign in to hub',
    hint: 'Enter the hub token. It is the AGENT_TOKEN value in ~/.guardian/agent.env on the hub.',
    signingInTo: 'Signing in to',
    tokenLabel: 'Hub token',
    invalid: 'The token is invalid or the hub is unreachable.',
    changeHub: 'Change hub',
    submit: 'Sign in',
  },
  settingsDialog: {
    title: 'Hub connection',
    urlLabel: 'Hub URL',
    invalidUrl: 'Enter a full http(s) URL, for example http://hub:9870.',
    signOut: 'Sign out',
    saveAndReconnect: 'Save and reconnect',
  },
  settingsPage: {
    sectionsAria: 'Settings sections',
    flowSectionsAria: 'Flow setup items',
    sections: {
      endpoint: 'Hub connection',
      flowConfig: 'Flow setup',
      workers: 'Execution environments',
      display: 'Display',
      projects: 'Projects',
      flows: 'Flows',
      personas: 'Characters',
    },
    endpointTitle: 'Hub connection',
    changeEndpoint: 'Change hub URL or sign out',
    groupsNotePrefix: 'Node groups are on the ',
    groupsNoteLink: 'Groups',
    groupsNoteSuffix: ' tab of Nodes.',
  },
  language: {
    label: 'Language',
    hint: 'Changing it reloads the page. Saved in this browser only.',
    // Each language in its own name, the same in every catalog.
    names: { ko: '한국어', en: 'English' } as Record<string, string>,
    saveFailed: 'The language cannot be saved in this browser (private window or blocked storage).',
  },

  confirm: { cancel: 'Cancel' },
  cancelDebate: {
    cancelling: 'Cancelling…',
    cancel: 'Cancel',
    titleDebate: 'Cancel debate',
    titleWiki: 'Cancel code wiki build',
    messageDebate:
      'Stop this debate now? The debate agents stop and the debate ends without a decision. Messages written so far are kept.',
    messageWiki: 'Stop this build now? The agents stop; pages already written are kept and the rest stay as they are.',
    confirmDebate: 'Cancel debate',
    confirmWiki: 'Cancel build',
    keep: 'Keep running',
  },
  dataGate: {
    loading: (what: string) => `Loading ${what}…`,
    couldNotLoad: (what: string) => `Couldn't load ${what}`,
    checkHub: 'Check the hub connection and your sign-in, then retry.',
    retry: 'Retry',
    loadingDot: 'Loading',
  },
  dataTable: { empty: 'Nothing to show.' },
  diff: { label: 'Unified diff' },
  projectSwitcher: {
    label: 'Project',
    invalid: ' - misconfigured',
  },

  // API / hub request errors thrown by api.ts and shown to the user
  api: {
    authRequired: 'Sign-in required',
    cannotReach: (detail: string) => `Can't reach the hub: ${detail}`,
    commandNotRun: (detail: string) => `Couldn't run the command: ${detail}`,
    streamEnded: 'The stream ended before the run finished',
    routeMissing: (base: string, path: string) =>
      `The hub agent at ${base} does not serve ${path}. It may be an older version: update the hub agent or reconnect the hub in settings.`,
  },

  // lib/audit.ts, lib/tones.ts, lib/reachability.ts display labels
  audit: {
    route: { revokeKey: 'Revoke key', node: 'Node', local: 'Local', group: 'Group', fleet: 'Fleet' },
    allExitOk: (n: number) => (n === 1 ? '1 run, exit 0' : `All ${n} exit 0`),
    failedOf: (failed: number, total: number) => `${failed} of ${total} failed`,
    status: { ok: 'Success', rejected: 'Rejected', error: 'Error' },
  },
  tones: {
    failed: (n: number) => `${n} failed`,
    warnings: (n: number) => `${n} ${n === 1 ? 'warning' : 'warnings'}`,
    info: (n: number) => `${n} info`,
    passed: (n: number) => `${n} passed`,
  },
  reachability: {
    ok: 'Online',
    commandFailed: 'Connected, check failed',
    authFailed: 'Key rejected',
    hostKeyChanged: 'Host key changed',
    unreachable: 'Unreachable',
    offline: 'Offline',
  },

  // Texts of the @server-guardian/ui components (installed by i18n/uiMessages.ts)
  ui: {
    copy: 'Copy',
    copied: 'Copied',
    navPrimary: 'Main menu',
    segmentedOptions: 'Options',
    meterUsage: 'Usage',
    verdictChain: 'Verdict chain',
    actionAria: (id: string) => `Action ${id}`,
    untitledAction: 'Untitled action',
    approve: 'Approve',
    reject: 'Reject',
    riskLabel: (risk: string) => `Risk ${risk}`,
    confirmDialog: {
      title: 'Confirm run',
      confirm: 'Confirm',
      cancel: 'Cancel',
      typeToConfirm: (text: string) => `Type the following to confirm: ${text}`,
    },
    heatmap: {
      rowLabel: 'Node',
      scoreLabel: 'Score',
      legend: 'Legend',
      check: (n: number) => `Check ${n}`,
      words: { P: 'Pass', W: 'Warning', F: 'Fail', I: 'Info', N: 'Not checked' },
    },
    nodeTable: {
      label: 'Fleet nodes',
      node: 'Node',
      status: 'Status',
      disk: 'Disk',
      load: 'Load',
      unavailable: (metric: string) => `${metric} unavailable`,
      sshScore: (score: number) => `SSH score ${score}`,
      online: 'Online',
      offline: 'Offline',
    },
    processTable: { user: 'User', command: 'Command' },
    terminal: { sessions: 'Terminal sessions', output: 'Terminal output', command: 'Command input', placeholder: 'Run a command' },
    commandPalette: {
      label: 'Command palette',
      search: 'Search commands',
      scope: 'Search scope',
      results: 'Results',
      noResults: 'No results',
      placeholder: 'Search nodes, actions, debates',
    },
  },
};
