import { StatusChip } from '@server-guardian/ui';
import { Caption, Card } from '../../components/Card';
import { MonoBlock } from '../../components/MonoBlock';
import { Notice } from '../../components/Notice';
import { Row, Stack, mono, muted } from '../../components/layout';
import { seatTone } from '../../lib/personas';
import { tr } from '../../i18n';
import type { PersonaConfig } from '../../types';

/** One item of a list field (expertise, checks, ...), wrapped so long items do not stretch the card. */
function Items({ items, monoItems = false }: { items: string[]; monoItems?: boolean }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 'var(--sg-s-8)', display: 'grid', gap: 'var(--sg-s-2)' }}>
      {items.map((item, i) => (
        <li key={i} style={{ overflowWrap: 'anywhere', fontSize: 13, ...(monoItems ? mono : {}) }}>
          {item}
        </li>
      ))}
    </ul>
  );
}

/**
 * Read-only view of a character (format 2, agent-characters.md C2): the profile, seats, kinds, the five persona
 * fields, tools, the role card and the rendered text the engine sends. The character editor is phase C4; until
 * then the dashboard shows characters and never writes them, so this view has no inputs.
 */
export function CharacterReadonly({ persona }: { persona: PersonaConfig }) {
  const P = tr.personas;
  const C = P.character;
  const block = persona.persona;
  const seats = persona.seats ?? [];
  const kinds = persona.kinds ?? [];
  const tools = persona.tools ?? [];
  const boundaries = block?.boundaries ?? [];
  return (
    <Stack gap="var(--sg-s-6)">
      <Notice tone="info">{C.readOnlyNotice}</Notice>
      <Card>
        <Row style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
          <strong style={{ overflowWrap: 'anywhere' }}>{persona.name}</strong>
          <span style={{ ...mono, ...muted, fontSize: 12, overflowWrap: 'anywhere' }}>{persona.id}</span>
        </Row>
        {persona.profile?.tagline ? (
          <div style={{ color: 'var(--sg-text-secondary)', fontSize: 13, overflowWrap: 'anywhere' }}>{persona.profile.tagline}</div>
        ) : null}
      </Card>
      <Card>
        <Caption>{C.seats}</Caption>
        <Row gap="var(--sg-s-2)">
          {seats.length === 0 ? <span style={{ ...muted, fontSize: 13 }}>{C.none}</span> : null}
          {seats.map((s) => (
            <StatusChip key={s} tone={seatTone(s)}>
              {P.slot[s] ?? s}
            </StatusChip>
          ))}
        </Row>
        <Caption>{C.kinds}</Caption>
        {kinds.length === 0 ? (
          <span style={{ ...muted, fontSize: 13 }}>{C.kindsAny}</span>
        ) : (
          <Row gap="var(--sg-s-2)">
            {kinds.map((k) => (
              <StatusChip key={k} tone="idle">
                {P.suggest.kinds[k] ?? k}
              </StatusChip>
            ))}
          </Row>
        )}
      </Card>
      <Card>
        <Caption>{C.identity}</Caption>
        <div style={{ fontSize: 13, overflowWrap: 'anywhere' }}>{block?.identity ?? ''}</div>
        <Caption>{C.expertise}</Caption>
        <Items items={block?.expertise ?? []} />
        <Caption>{C.stance}</Caption>
        <div style={{ fontSize: 13, overflowWrap: 'anywhere' }}>{block?.stance ?? ''}</div>
        <Caption>{C.checks}</Caption>
        <Items items={block?.checks ?? []} />
        {boundaries.length > 0 ? (
          <>
            <Caption>{C.boundaries}</Caption>
            <Items items={boundaries} />
          </>
        ) : null}
      </Card>
      <Card>
        <Caption>{C.tools}</Caption>
        {tools.length === 0 ? <span style={{ ...muted, fontSize: 13 }}>{C.none}</span> : <Items items={tools} monoItems />}
        <Caption>{C.roleCard}</Caption>
        {persona.role_card ? (
          <div style={{ fontSize: 13 }}>
            {P.slot[persona.role_card]}
            {persona.role_card_kind ? <span style={{ ...mono, ...muted }}>{` (${persona.role_card_kind})`}</span> : null}
          </div>
        ) : (
          <span style={{ ...muted, fontSize: 13 }}>{C.none}</span>
        )}
      </Card>
      <div style={{ display: 'grid', gap: 'var(--sg-s-2)' }}>
        <Caption>{C.rendered}</Caption>
        <MonoBlock text={persona.rendered ?? ''} />
      </div>
    </Stack>
  );
}
