import '@server-guardian/ui/styles.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { GuardianRoot } from '@server-guardian/ui';
import { App } from './App';
import './i18n/uiMessages';

// GuardianRoot paints its own div; make the page behind it match so overscroll never flashes white.
document.body.style.margin = '0';
document.body.style.background = 'var(--sg-bg)';
document.documentElement.style.colorScheme = 'dark';

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <GuardianRoot style={{ minHeight: '100vh' }}>
      <App />
    </GuardianRoot>
  </StrictMode>
);
