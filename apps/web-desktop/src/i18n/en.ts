/**
 * English message catalog of the web dashboard. Same shape as the Korean one (Messages, see ./types.ts): every key
 * and every function signature of ./ko.ts, with English words. Rules (raw values, interpolation, what stays in
 * English) and the glossary are in ./ko.ts; English UI copy is plain sentence case, labels without a final period.
 */
import type { Messages } from './types';
import { common } from './en/common';
import { shell } from './en/shell';
import { ops } from './en/ops';
import { fleet } from './en/fleet';
import { debate } from './en/debate';
import { agents } from './en/agents';
import { wiki } from './en/wiki';
import { artifacts } from './en/artifacts';
import { personas } from './en/personas';
import { results } from './en/results';

export const en: Messages = { common, shell, ops, fleet, debate, agents, wiki, artifacts, personas, results };
