/**
 * Shared empty state.
 *
 * Uses a self-generated illustration (see frontend/public/assets/ASSETS.md).
 * The image is decorative and lazy-loaded, with explicit dimensions so it
 * cannot cause layout shift while loading.
 */

interface EmptyStateProps {
  image: string;
  title: string;
  body: string;
  testId?: string;
  action?: React.ReactNode;
}

export function EmptyState({ image, title, body, testId, action }: EmptyStateProps) {
  return (
    <div
      className="panel flex flex-col items-center px-xl py-huge text-center"
      data-testid={testId}
    >
      <img
        src={image}
        alt=""
        aria-hidden="true"
        width={300}
        height={200}
        loading="lazy"
        decoding="async"
        className="mb-xl h-auto w-[300px] max-w-full opacity-80"
      />
      <h3 className="text-display-md">{title}</h3>
      <p className="mt-md max-w-[52ch] text-body-md text-on-primary-mute">{body}</p>
      {action ? <div className="mt-xl">{action}</div> : null}
    </div>
  );
}
