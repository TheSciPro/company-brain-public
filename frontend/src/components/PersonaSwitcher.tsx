import { Badge } from "./ui/badge";
import type { Persona } from "../types";

interface Props {
  personas: Persona[];
  value: string;
  onChange: (id: string) => void;
}

// "Name · role" in the persona label; the switcher shows them apart.
const split = (label: string) => {
  const [name, ...role] = label.split(" · ");
  return { name, role: role.join(" · ") };
};

/** A native select (keyboard and test friendly) styled like shadcn/ui Select, plus the chosen person's role. */
export function PersonaSwitcher({ personas, value, onChange }: Props) {
  const chosen = personas.find((p) => p.id === value);
  const who = chosen ? split(chosen.label) : null;
  return (
    <div className="flex items-center gap-3">
      {who && (
        <div className="hidden text-right leading-tight md:block">
          <div className="text-sm font-semibold">{who.name}</div>
          <div className="text-xs text-slate-500">{who.role}</div>
        </div>
      )}
      {chosen && <Badge variant="secondary">{chosen.id}</Badge>}
      {chosen?.asof && <Badge variant="outline">as of {chosen.asof}</Badge>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Acting as"
        className="h-9 rounded-md border border-slate-200 bg-white px-3 pr-8 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
      >
        <option value="" disabled>
          Choose who you are…
        </option>
        {personas.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label} ({p.id})
          </option>
        ))}
      </select>
    </div>
  );
}
