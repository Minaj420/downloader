import {
  AUTO_PLATFORM_VALUE,
  PLATFORM_OPTIONS,
} from "./platforms";

interface AnalyzeFormProps {
  url: string;
  platform: string;
  isPending: boolean;
  onUrlChange: (url: string) => void;
  onPlatformChange: (platform: string) => void;
  onSubmit: () => void;
}

export function AnalyzeForm({
  url,
  platform,
  isPending,
  onUrlChange,
  onPlatformChange,
  onSubmit,
}: AnalyzeFormProps) {
  return (
    <form
      className="analyze-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="field">
        <label htmlFor="platform">Platform</label>
        <select
          id="platform"
          value={platform}
          onChange={(event) => onPlatformChange(event.target.value)}
        >
          <option value={AUTO_PLATFORM_VALUE}>Auto Select</option>
          {PLATFORM_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="url">URL</label>
        <input
          id="url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://…"
          value={url}
          onChange={(event) => onUrlChange(event.target.value)}
        />
      </div>
      <div className="form-actions">
        <button type="submit" disabled={isPending || url.trim().length === 0}>
          {isPending ? "Analyzing…" : "Analyze"}
        </button>
      </div>
    </form>
  );
}
