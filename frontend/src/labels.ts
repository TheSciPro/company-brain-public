// What the chat shows while the brain works: a plain-language step instead of internal tool names.
// Presentation only; an unknown tool falls back to its name with spaces.

const STEPS: Record<string, string> = {
  read_file: "reading notes",
  ls: "looking through notes",
  glob: "looking through notes",
  grep: "searching notes",
  edit_file: "updating your memory",
  write_file: "updating your memory",
  write_todos: "planning",
  company_fact: "checking company records",
  fact_history: "checking how this changed over time",
  search_company: "searching company documents",
  who_approves: "finding the approver",
  org_lookup: "looking up people",
  onboarding_brief: "checking your onboarding plan",
  task: "asking a specialist",
};

const SKILL_PREFIX = "/skills/";
export const MEMORY_PREFIX = "/memories/";

export function stepLabel(tool: string, args?: Record<string, unknown>): string {
  const path = typeof args?.file_path === "string" ? args.file_path : "";
  if (tool === "read_file" && path.startsWith(SKILL_PREFIX)) return "reading the playbook";
  if (tool === "read_file" && path.startsWith(MEMORY_PREFIX)) return "checking what I remember about you";
  if (tool.startsWith("query_")) return `checking ${tool.slice("query_".length).replace(/_/g, " ")}`;
  return STEPS[tool] ?? tool.replace(/_/g, " ");
}

// Why a classification graph was not opened, in the words a judge reads. "empty" (nothing there for this
// person) is not shown: it is not an access decision.
const NOT_OPENED: Record<string, string> = {
  access: "not cleared for this level",
  audience: "not shared with this audience",
  unavailable: "graph unavailable right now",
};

export function notOpenedReason(reason: string): string | null {
  return reason === "empty" ? null : (NOT_OPENED[reason] ?? reason);
}

export const levelLabel = (level: string): string => level.replace(/_/g, " ");
