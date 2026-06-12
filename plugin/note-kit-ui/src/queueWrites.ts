/** Shared queue-file write conventions — the ONE home for every mutation the
 * clean views make to the two queue files, and for the literal markers those
 * writes carry. Both NowView and QueueView route here. Every write goes through
 * `vault.process` (an atomic read-modify-write): the queue files are written
 * concurrently by the hourly action-agent, so a separate read→modify pair could
 * clobber a line that changed in between. */

import { TFile, Vault } from "obsidian";
import type { Decision, QueueItem } from "./nowView";

/** Markdown checkbox line: groups are (lead)(state)(mid)(text)(trail). */
export const CHECKBOX_RE = /^(\s*[-*]\s+\[)(.)(\]\s+)(.*\S)(\s*)$/;
/** A `##`/`###` heading line. */
export const HEADING_RE = /^(#{2,})\s+(.*\S)\s*$/;
/** The literal a user cross-off writes beside its [x] — a skip, not a completion. */
export const SKIP_MARK_RE = /\s*\*\(item skipped\)\*$/;
/** The literal the action-agent writes beside its [x] — a completion receipt. */
export const EXEC_MARK_RE = /\s*\*\(executed\)\*$/;

const SKIP_MARK = "*(item skipped)*";
/** The sanctioned acknowledge option (Format-User-Queue) — written checked, so a
 * non-choice notification resolves like a picked decision. */
const ACK_OPTION = "Acknowledged — clear this item";

/** Collapse embedded newlines to one line before a checklist write — a mobile
 * return key would otherwise split one item into a truncated action plus inert
 * prose lines. */
export function collapseToLine(text: string): string {
	return text.replace(/\s*\n\s*/g, " ").trim();
}

/** Atomic line-level edit: `vault.process` re-reads inside the write lock, so a
 * concurrent agent write can't land between the read and the write. */
async function processLines(vault: Vault, file: TFile, edit: (lines: string[]) => void): Promise<void> {
	await vault.process(file, (content) => {
		const lines = content.split("\n");
		edit(lines);
		return lines.join("\n");
	});
}

/** Cross off / restore a machine-queue item, located by its text (robust to the
 * file shifting since the read). Crossing off writes the "*(item skipped)*"
 * marker beside the [x] so the agent's sweep tells it from a real completion;
 * restoring strips both the skip and executed markers. */
export async function toggleSkip(vault: Vault, file: TFile, item: QueueItem): Promise<void> {
	await processLines(vault, file, (lines) => {
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === item.text) {
				lines[i] = item.done
					? `${m[1]} ${m[3]}${item.text.replace(SKIP_MARK_RE, "").replace(EXEC_MARK_RE, "")}${m[5]}`
					: `${m[1]}x${m[3]}${item.text} ${SKIP_MARK}${m[5]}`;
				break;
			}
		}
	});
}

/** Append a new open task at the end of the checklist. */
export async function appendTask(vault: Vault, file: TFile, text: string): Promise<void> {
	const line = collapseToLine(text);
	await vault.process(file, (content) => `${content.replace(/\s*$/, "")}\n- [ ] ${line}\n`);
}

/** Replace an open task's text in place, re-located by its current text,
 * keeping its checkbox state. */
export async function updateTask(vault: Vault, file: TFile, oldText: string, newText: string): Promise<void> {
	const line = collapseToLine(newText);
	await processLines(vault, file, (lines) => {
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === oldText) {
				lines[i] = `${m[1]}${m[2]}${m[3]}${line}${m[5]}`;
				break;
			}
		}
	});
}

/** Toggle a checkbox by matching its text, not a stored line index. */
export async function setChecked(vault: Vault, file: TFile, text: string, checked: boolean): Promise<void> {
	await processLines(vault, file, (lines) => {
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === text) {
				lines[i] = `${m[1]}${checked ? "x" : " "}${m[3]}${m[4]}${m[5]}`;
				break;
			}
		}
	});
}

/** Approve a fill-in option: replace its placeholder text and check it in one write. */
export async function pickOptionFilled(vault: Vault, file: TFile, rawText: string, filledText: string): Promise<void> {
	await processLines(vault, file, (lines) => {
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === rawText) {
				lines[i] = `${m[1]}x${m[3]}${filledText}${m[5]}`;
				break;
			}
		}
	});
}

/** Acknowledge a non-choice notification — append the sanctioned option text
 * checked (`- [x] Acknowledged — clear this item`) at the end of its heading
 * block, so it parses as a resolved decision and the action-agent clears it
 * next pass. Needs a heading to locate the write target. */
export async function acknowledge(vault: Vault, file: TFile, d: Decision): Promise<void> {
	const title = d.title;
	if (!title) return;
	await processLines(vault, file, (lines) => {
		let i = 0;
		let found = false;
		for (; i < lines.length; i++) {
			const hm = lines[i].match(HEADING_RE);
			if (hm && hm[2] === title) {
				found = true;
				i++;
				break;
			}
		}
		if (!found) return;
		// Insert after the heading's prose block, before the next heading or EOF.
		let at = i;
		for (; i < lines.length; i++) {
			if (lines[i].match(HEADING_RE)) break;
			if (lines[i].trim()) at = i + 1;
		}
		lines.splice(at, 0, `- [x] ${ACK_OPTION}`);
	});
}

/** Append a new option line beneath a decision's heading. */
export async function appendOption(vault: Vault, file: TFile, d: Decision, text: string): Promise<void> {
	const line = collapseToLine(text);
	await processLines(vault, file, (lines) => {
		const at = lastOptionLine(lines, d);
		if (at < 0) return;
		lines.splice(at + 1, 0, `- [ ] ${line}`);
	});
}

/**
 * Line index of decision `d`'s last option, re-located by its heading then the
 * contiguous run of checkboxes beneath — so an appended option lands in the right
 * block even if line numbers shifted since the read.
 */
function lastOptionLine(lines: string[], d: Decision): number {
	let i = 0;
	if (d.title) {
		for (; i < lines.length; i++) {
			const hm = lines[i].match(HEADING_RE);
			if (hm && hm[2] === d.title) {
				i++;
				break;
			}
		}
		if (i >= lines.length) return -1;
	}
	let last = -1;
	let started = false;
	for (; i < lines.length; i++) {
		if (lines[i].match(HEADING_RE)) break;
		if (lines[i].match(CHECKBOX_RE)) {
			last = i;
			started = true;
		} else if (started && lines[i].trim()) {
			break; // the option run ended
		}
	}
	return last;
}
