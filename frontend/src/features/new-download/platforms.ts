import bandcampIcon from "../../assets/platforms/bandcamp.svg";
import instagramIcon from "../../assets/platforms/instagram.svg";
import redditIcon from "../../assets/platforms/reddit.svg";
import soundcloudIcon from "../../assets/platforms/soundcloud.svg";
import tiktokIcon from "../../assets/platforms/tiktok.svg";
import twitchIcon from "../../assets/platforms/twitch.svg";
import vimeoIcon from "../../assets/platforms/vimeo.svg";
import xIcon from "../../assets/platforms/x.svg";
import youtubeIcon from "../../assets/platforms/youtube.svg";
import youtubeMusicIcon from "../../assets/platforms/youtubemusic.svg";

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

// Shorts are detected as "youtube" and share its icon; "other"/unknown values
// have no entry and stay text only.
const PLATFORM_ICONS: Readonly<Record<string, string>> = {
  youtube: youtubeIcon,
  youtube_music: youtubeMusicIcon,
  soundcloud: soundcloudIcon,
  bandcamp: bandcampIcon,
  vimeo: vimeoIcon,
  twitch: twitchIcon,
  tiktok: tiktokIcon,
  instagram: instagramIcon,
  twitter: xIcon,
  reddit: redditIcon,
};

export function platformIconUrl(value: string): string | null {
  return Object.prototype.hasOwnProperty.call(PLATFORM_ICONS, value)
    ? PLATFORM_ICONS[value]
    : null;
}

export function platformLabel(value: string): string {
  return PLATFORM_OPTIONS.find((option) => option.value === value)?.label ?? value;
}
