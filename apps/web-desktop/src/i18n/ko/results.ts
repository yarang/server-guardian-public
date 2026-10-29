// Korean copy: project results tab. Rules and glossary: see ../ko.ts.
export const results = {
  listTitle: (n: number | null) => (n === null ? '결과물' : `결과물 (${n})`),
  listWhat: '결과물 목록',
  listAria: '프로젝트 결과물',
  empty: '아직 결과물이 없어요. 토론이 끝나면 결과 문서가 여기에 쌓이고, 설계 프로젝트는 산출물도 함께 보여요.',
  capped: (shown: number, total: number) => `최근 ${shown}건만 보여요 (전체 ${total}건).`,
  openThread: (title: string) => `결과 ${title} 열기`,
  backToList: '← 결과 목록으로',
  rounds: (n: number) => `${n}라운드`,
  artifactCount: (versions: number, files: number) => `산출물 버전 ${versions}개 · 파일 ${files}개`,
  noReport: '결과 문서 없음',
  threadId: (id: string) => `토론 ${id}`,
  openArtifacts: '산출물 보기',
  openArtifactsTitle: (thread: string) => `토론 ${thread}가 남긴 문서와 다이어그램 열기`,
  detailTitle: (title: string) => `결과: ${title}`,
  reportMissing: '이 토론에는 결과 문서가 없고 산출물만 있어요.',
} as const;
