import { StatusChip } from '@server-guardian/ui';
import { tr } from '../../i18n';
import { isCharacter } from '../../lib/personas';
import type { PersonaConfig } from '../../types';

/** Format chip beside the source chip: legacy (format 1, free system prompt) or character (format 2, field by field). */
export function FormatChip({ persona }: { persona: PersonaConfig }) {
  if (isCharacter(persona)) return <StatusChip tone="ok">{tr.personas.character.chip}</StatusChip>;
  return (
    <StatusChip tone="idle" title={tr.personas.character.legacyChipTitle}>
      {tr.personas.character.legacyChip}
    </StatusChip>
  );
}
