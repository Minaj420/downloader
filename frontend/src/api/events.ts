import type { ActivityEvent } from "./activity";
import type { Job } from "./jobs";

export type Unsubscribe = () => void;

const JOB_EVENT_TYPES = [
  "job.updated",
  "job.completed",
  "job.failed",
  "job.cancelled",
] as const;

const ACTIVITY_EVENT_TYPE = "activity.created";

type JobEventType = (typeof JOB_EVENT_TYPES)[number];

function isJobEventType(type: string): type is JobEventType {
  return (JOB_EVENT_TYPES as readonly string[]).includes(type);
}

/**
 * Subscribes to live job updates over SSE; returns an unsubscribe function.
 * The optional `onActivityEvent` additionally receives `activity.created`
 * events on the same connection — no second EventSource is opened. The
 * browser's EventSource handles reconnection and Last-Event-ID.
 */
export function subscribeToJobUpdates(
  onJobUpdate: (job: Job) => void,
  onError?: () => void,
  onActivityEvent?: (event: ActivityEvent) => void,
  onOpen?: () => void,
): Unsubscribe {
  const source = new EventSource("/api/v1/events");
  const handleEvent = (event: Event) => {
    const message = event as MessageEvent<string>;
    if (!isJobEventType(message.type)) {
      return;
    }
    try {
      onJobUpdate(JSON.parse(message.data) as Job);
    } catch {
      return;
    }
  };
  for (const type of JOB_EVENT_TYPES) {
    source.addEventListener(type, handleEvent);
  }
  if (onActivityEvent != null) {
    source.addEventListener(ACTIVITY_EVENT_TYPE, (event: Event) => {
      const message = event as MessageEvent<string>;
      try {
        onActivityEvent(JSON.parse(message.data) as ActivityEvent);
      } catch {
        return;
      }
    });
  }
  source.onopen = () => onOpen?.();
  source.onerror = () => onError?.();
  return () => {
    source.close();
  };
}
