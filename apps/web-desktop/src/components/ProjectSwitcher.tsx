import { tr } from '../i18n';
import { SelectField } from './SelectField';
import type { SelectOption } from './SelectField';
import { useProjectStore } from '../state/projectStore';

/**
 * Select for the project Debate, Approvals and Overview work on (app-level, persisted). Projects
 * with configuration errors are listed but disabled; the selected id is always an option, even
 * while the project list is loading or could not be loaded.
 */
export function ProjectSwitcher() {
  const { selected, select, projects } = useProjectStore();
  const options: SelectOption[] = (projects.data?.projects ?? []).map((p) => ({
    value: p.id,
    label: `${p.data.name || p.id} (${p.id})${p.errors.length ? tr.shell.projectSwitcher.invalid : ''}`,
    disabled: p.errors.length > 0 && p.id !== selected,
  }));
  if (!options.some((o) => o.value === selected)) options.unshift({ value: selected, label: selected });
  return <SelectField label={tr.shell.projectSwitcher.label} value={selected} onChange={(e) => select(e.target.value)} options={options} />;
}
