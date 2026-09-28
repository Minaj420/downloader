export interface PlatformOption {
  value: string;
  label: string;
}

export const AUTO_PLATFORM_VALUE = "auto";

export const PLATFORM_OPTIONS: readonly PlatformOption[] = [
  { value: "youtube", label: "YouTube" },
  { value: "youtube_music", label: "YouTube Music" },
  { value: "soundcloud", label: "SoundCloud" },
  { value: "bandcamp", label: "Bandcamp" },
  { value: "vimeo", label: "Vimeo" },
  { value: "twitch", label: "Twitch" },
  { value: "tiktok", label: "TikTok" },
  { value: "instagram", label: "Instagram" },
  { value: "twitter", label: "X / Twitter" },
  { value: "reddit", label: "Reddit" },
  { value: "other", label: "Other" },
];

export function platformLabel(value: string): string {
  return PLATFORM_OPTIONS.find((option) => option.value === value)?.label ?? value;
}
