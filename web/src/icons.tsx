/** Icons drawn for Lectern where lucide-react has none that says what is meant. They follow
 * lucide's conventions (a 24-unit box, a 2-unit round stroke in the text colour), so they sit
 * beside its icons, and are hidden from screen readers: the button around one names it. */

/** Make a picture: a picture frame with a sparkle in its open corner. lucide's picture icons
 * all read as "add a picture", which beside the paperclip would look like a second Attach. */
export function ImageSparkleIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="m3 17 5-5 9 9" />
      <circle cx="8.5" cy="7.5" r="1.5" />
      <path
        d="M18 1.5q.5 3 3.5 3.5-3 .5-3.5 3.5-.5-3-3.5-3.5 3-.5 3.5-3.5z"
        fill="currentColor"
        strokeWidth={1}
      />
    </svg>
  );
}
