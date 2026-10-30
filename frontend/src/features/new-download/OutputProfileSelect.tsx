import {
  outputProfilesFor,
  type OutputMediaKind,
  type OutputProfile,
} from "../../api/jobs";

interface OutputProfileSelectProps {
  kind: OutputMediaKind;
  value: OutputProfile;
  disabled: boolean;
  onChange: (profile: OutputProfile) => void;
}

const PROFILE_LABELS: Record<OutputMediaKind, Partial<Record<OutputProfile, string>>> = {
  audio: {
    original: "Original / Best Available",
    mp3: "MP3",
    wav: "WAV",
    m4a: "M4A",
    opus: "Opus",
    flac: "FLAC",
  },
  video: {
    original: "Original",
    mp4: "MP4 (compatible)",
  },
};

/** Conversion target for the new job; independent of the source variant. */
export function OutputProfileSelect({
  kind,
  value,
  disabled,
  onChange,
}: OutputProfileSelectProps) {
  return (
    <fieldset className="destination-group">
      <legend>Output format</legend>
      {outputProfilesFor(kind).map((profile) => (
        <label key={profile} className="destination-option">
          <input
            type="radio"
            name="output-profile"
            value={profile}
            checked={value === profile}
            disabled={disabled}
            onChange={() => onChange(profile)}
          />
          {PROFILE_LABELS[kind][profile]}
        </label>
      ))}
      <p className="muted-note">
        Conversion is for compatibility, not a quality upgrade. WAV and FLAC do
        not restore detail missing from the source.
      </p>
    </fieldset>
  );
}
