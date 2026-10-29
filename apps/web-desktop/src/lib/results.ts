/** Route params of the Results tab: `#/debates/results[/<thread>]`. */
export interface ResultsRoute {
  thread?: string;
}

const THREAD = /^thread_[A-Za-z0-9_]{1,80}$/;

export function parseResultsRoute(params: string[]): ResultsRoute {
  const [thread] = params;
  return thread && THREAD.test(thread) ? { thread } : {};
}

export function resultsParams(r: ResultsRoute): string[] {
  return r.thread ? ['results', r.thread] : ['results'];
}
