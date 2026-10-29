/** Semantic status tone shared by dots, chips, meters, sparklines and captions. */
export type Tone = 'ok' | 'warn' | 'crit' | 'info' | 'idle';

/** Risk level of a proposed action. */
export type Risk = 'LOW' | 'MEDIUM' | 'HIGH';

/** Map a risk level to the tone used to render it. */
export const RISK_TONE: Record<Risk, Tone> = {
  LOW: 'ok',
  MEDIUM: 'warn',
  HIGH: 'crit',
};
