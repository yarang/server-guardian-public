import { setUiMessages } from '@server-guardian/ui';
import { tr } from './index';

/** Makes the active language the app-wide text of the design-system components (copy button, table headers, aria labels, ...). */
export function installUiMessages(): void {
  setUiMessages(tr.shell.ui);
}

installUiMessages();
