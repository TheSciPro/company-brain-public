import type { NotSearched, Source } from "./types";

/** One chat turn. A brain turn carries the evidence it was built from, for the Evidence panel. */
export interface Turn {
  role: "user" | "brain";
  text: string;
  question?: string;
  step?: string;
  steps: string[];
  sources: Source[];
  searched: string[];
  notSearched: NotSearched[];
  /** True when the brain read this person's saved memory (not shown as evidence; a memory-only answer has none). */
  memory: boolean;
  status?: string;
}
