"use client";

import { useRouter } from "next/navigation";
import { useId, useState, type FormEvent } from "react";
import styles from "./research.module.css";

export type PickerOption = { id: string; label: string; href: string };

/** Find a player in the pool by name and select (or compare) them. Plain form + datalist: works with a
 *  keyboard and on phones; the selection lands in the URL so the view can be shared. */
export function PlayerPicker({ options, label, placeholder, action }: {
  options: PickerOption[];
  label: string;
  placeholder: string;
  action: string;
}) {
  const router = useRouter();
  const id = useId();
  const [value, setValue] = useState("");
  const [miss, setMiss] = useState(false);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = value.trim().toLowerCase();
    if (!q) return;
    const hit = options.find((o) => o.label.toLowerCase() === q)
      ?? options.find((o) => o.label.toLowerCase().startsWith(q))
      ?? options.find((o) => o.label.toLowerCase().includes(q));
    setMiss(!hit);
    if (hit) router.push(hit.href, { scroll: false });
  };
  return (
    <form className={styles.picker} onSubmit={submit} role="search">
      <label htmlFor={`${id}-q`} className={styles.pickerLabel}>{label}</label>
      <div className={styles.pickerRow}>
        <input id={`${id}-q`} type="search" list={`${id}-list`} value={value} placeholder={placeholder} autoComplete="off"
               aria-invalid={miss || undefined} aria-describedby={miss ? `${id}-miss` : undefined}
               onChange={(e) => { setValue(e.target.value); setMiss(false); }} />
        <datalist id={`${id}-list`}>
          {options.map((o) => <option key={o.id} value={o.label} />)}
        </datalist>
        <button type="submit">{action}</button>
      </div>
      {miss && <p id={`${id}-miss`} className={styles.pickerMiss} role="status">No player in the current pool matches. Relax the filters to widen it.</p>}
    </form>
  );
}
