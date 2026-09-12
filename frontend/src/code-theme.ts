/** code-theme.ts — CodeMirror dressed in Seymour's own variables.
 *
 * Every color is a CSS variable from styles.css, so the editor follows
 * the theme (dark/light, accent hue) with no re-theming: style-mod emits
 * the var() text verbatim and the browser resolves it live.
 */

import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";

export const seymourTheme = EditorView.theme({
  "&": { background: "var(--bg)", color: "var(--ink)", height: "100%",
         font: "12px/1.5 ui-monospace, Menlo, monospace" },
  ".cm-scroller": { overflow: "auto", fontFamily: "ui-monospace, Menlo, monospace" },
  ".cm-content": { caretColor: "var(--accent)" },
  ".cm-gutters": { background: "var(--panel)", color: "var(--muted)",
                   borderRight: "2px solid var(--line)" },
  ".cm-activeLine, .cm-activeLineGutter": { background: "hsl(var(--accent-h) 40% 22% / .25)" },
  ".cm-cursor, .cm-dropCursor": { borderLeft: "2px solid var(--accent)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { background: "var(--accent-soft)" },
  ".cm-matchingBracket": { outline: "1px solid var(--accent)", background: "transparent" },
  ".cm-searchMatch": { background: "hsl(40 70% 55% / .35)" },
  ".cm-panels": { background: "var(--panel)", borderTop: "2px solid var(--line)", color: "var(--ink)" },
  ".cm-tooltip": { background: "var(--card)", border: "2px solid var(--line)",
                   boxShadow: "3px 3px 0 var(--line)", color: "var(--ink)" },
  "&.cm-focused": { outline: "none" },
});

export const seymourHighlight = syntaxHighlighting(HighlightStyle.define([
  { tag: t.comment, color: "var(--muted)", fontStyle: "italic" },
  { tag: [t.keyword, t.tagName, t.standard(t.name), t.operatorKeyword], color: "var(--accent)" },
  { tag: [t.string, t.attributeName, t.attributeValue], color: "hsl(var(--avatar-h) 50% 60%)" },
  { tag: [t.number, t.bool, t.null, t.literal], color: "hsl(40 70% 55%)" },
  { tag: [t.function(t.variableName), t.definition(t.name), t.typeName, t.className],
    color: "var(--ink)", fontWeight: "700" },
  { tag: [t.propertyName], color: "var(--ink)" },
  { tag: t.invalid, color: "var(--danger)" },
]));
