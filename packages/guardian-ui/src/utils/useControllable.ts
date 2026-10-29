import { useCallback, useState } from 'react';

/**
 * Support both controlled (`value` provided) and uncontrolled (`defaultValue`) usage.
 * Returns the current value and a setter that updates internal state when uncontrolled.
 */
export function useControllable<T>(
  value: T | undefined,
  defaultValue: T,
): [T, (next: T) => void] {
  const [inner, setInner] = useState<T>(defaultValue);
  const isControlled = value !== undefined;
  const set = useCallback(
    (next: T) => {
      if (!isControlled) setInner(next);
    },
    [isControlled],
  );
  return [isControlled ? (value as T) : inner, set];
}
