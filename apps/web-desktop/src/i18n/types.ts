import type { Catalog } from './ko';

/**
 * The shape every language catalog must have: the Korean catalog with its string literal types widened to string,
 * so another language has the same keys and the same function signatures (arguments and return type) with its own
 * words. A missing, extra or mistyped key in a translation is a compile error.
 */
type Widen<T> = T extends string
  ? string
  : T extends (...args: infer A) => infer R
    ? (...args: A) => Widen<R>
    : T extends readonly (infer U)[]
      ? readonly Widen<U>[]
      : T extends object
        ? { readonly [K in keyof T]: Widen<T[K]> }
        : T;

export type Messages = Widen<Catalog>;
