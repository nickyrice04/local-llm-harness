/** dom.ts — one tiny helper instead of a framework.
 *
 * el("button.primary", {onclick}, "Send") creates an element, applies
 * props, and appends children. Everything is textContent under the hood:
 * NO innerHTML anywhere in this app, because model output is untrusted
 * and must never be parsed as markup (the XSS rule from the guide).
 */

/** The props views actually use, typed plainly. `style` is a CSS string
 *  (the DOM's own style property is read-only, so el() applies it via
 *  setAttribute); everything else assigns straight onto the element. */
interface Props extends Record<string, unknown> {
  style?: string;
  className?: string;
  id?: string;
  title?: string;
  value?: string;
  placeholder?: string;
  autocomplete?: string;
  hidden?: boolean;
  rows?: number;
  onclick?: (event: Event) => void;
  onsubmit?: (event: Event) => void;
}

/** Create an element from a "tag.class1.class2" spec. */
export function el<K extends keyof HTMLElementTagNameMap>(
  spec: `${K}` | `${K}.${string}`,
  props: Props = {},
  ...children: (Node | string | null | undefined)[]
): HTMLElementTagNameMap[K] {
  // Split "div.card.row" into the tag and its classes.
  const [tag, ...classes] = spec.split(".");
  const node = document.createElement(tag as K);
  if (classes.length) node.className = classes.join(" ");
  // `style` as a string needs setAttribute (the property is read-only);
  // everything else assigns directly (onclick, value, placeholder, …).
  const { style, ...rest } = props as Record<string, unknown>;
  if (typeof style === "string") node.setAttribute("style", style);
  Object.assign(node, rest);
  // Append children; bare strings become TEXT nodes — never markup.
  for (const child of children) {
    if (child == null) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** Replace a container's children (the "render this view" primitive).
 *  Nulls are simply skipped, so views can write `cond ? el(…) : null`. */
export function mount(
  container: HTMLElement,
  ...children: (Node | string | null | undefined)[]
): void {
  container.replaceChildren(
    ...children.filter((c): c is Node | string => c != null));
}
