import { createContext, useContext } from 'react';

/** Bumped by the header Refresh button (and after login / endpoint change); data hooks refetch on change. */
export const RefreshContext = createContext(0);

export function useRefreshToken(): number {
  return useContext(RefreshContext);
}
