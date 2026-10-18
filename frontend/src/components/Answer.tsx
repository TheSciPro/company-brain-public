import type { ReactNode } from "react";

// Light rendering for the brain's replies: paragraphs, bullet and numbered lists, **bold**, `code`, and
// citations ([id] or 【id】) as small chips. No HTML from the model is ever injected.

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|【[^】]+】|\[[^\]\s][^\]]*\])/g;

function inline(text: string, onCite?: (id: string) => void): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.startsWith("【") || (part.startsWith("[") && part.endsWith("]")))
    {
      const id = part.slice(1, -1).replace(/^</, "").replace(/>$/, "");
      return (
        <code key={i} className="cite" role="button" title="Show evidence" onClick={() => onCite?.(id)}>
          {id}
        </code>
      );
    }
    return part;
  });
}

const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d+[.)]\s+/;

export function Answer({ text, onCite }: { text: string; onCite?: (id: string) => void }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it, onCite)}</li>);
    blocks.push(list.ordered ? <ol key={blocks.length}>{items}</ol> : <ul key={blocks.length}>{items}</ul>);
    list = null;
  };
  for (const line of text.split("\n")) {
    const ordered = NUMBERED.test(line);
    if (ordered || BULLET.test(line)) {
      if (!list || list.ordered !== ordered) {
        flush();
        list = { ordered, items: [] };
      }
      list.items.push(line.replace(ordered ? NUMBERED : BULLET, ""));
      continue;
    }
    flush();
    if (line.trim()) blocks.push(<p key={blocks.length}>{inline(line, onCite)}</p>);
  }
  flush();
  return <div className="answer">{blocks}</div>;
}
