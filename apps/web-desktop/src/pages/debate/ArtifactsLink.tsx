import { tr } from '../../i18n';
import { DEFAULT_PROJECT } from '../../lib/project';
import { hrefFor } from '../../router';
import { useProjectStore } from '../../state/projectStore';

/**
 * "산출물 보기" link of a finished debate: opens the thread's artifacts. Shown only when the debate belongs to the
 * selected design project, because the Artifacts tab exists only for the selected project and only for `kind: "design"`.
 */
export function ArtifactsLink({ thread, project }: { thread: string; project?: string }) {
  const { selected, projects } = useProjectStore();
  const owner = project || DEFAULT_PROJECT;
  const entry = projects.data?.projects.find((p) => p.id === owner);
  if (owner !== selected || entry?.data.kind !== 'design') return null;
  return (
    <a href={hrefFor('debates', ['artifacts', thread])} title={tr.artifacts.openFromResultTitle(thread)} style={{ fontSize: 13 }}>
      {tr.artifacts.openFromResult}
    </a>
  );
}
