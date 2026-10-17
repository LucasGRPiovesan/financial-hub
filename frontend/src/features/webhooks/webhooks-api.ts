/*
 * Outbound webhook endpoints: typed query/mutation layer over the generated request functions.
 *
 * The signing secret (`whsec_…`) comes back exactly once, from create and from rotate. TanStack Query keeps a
 * mutation's result in the mutation cache, so those two mutations hand the secret to `onSecret` and resolve
 * with the endpoint view only: the plaintext never enters a cache (same rule as the API key plaintext).
 */
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import { getGetProjectEventV1ProjectsProjectIdEventsEventIdGetUrl } from "../../api/generated/endpoints/events/events";
import {
  createWebhookEndpointV1ProjectsProjectIdWebhookEndpointsPost,
  disableWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdDisablePost,
  getGetWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdGetUrl,
  getListWebhookAttemptsV1ProjectsProjectIdWebhookEndpointsEndpointIdAttemptsGetUrl,
  getListWebhookEndpointsV1ProjectsProjectIdWebhookEndpointsGetUrl,
  getWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdGet,
  listWebhookAttemptsV1ProjectsProjectIdWebhookEndpointsEndpointIdAttemptsGet,
  listWebhookEndpointsV1ProjectsProjectIdWebhookEndpointsGet,
  retryWebhookDeliveryV1ProjectsProjectIdEventsEventIdDeliveriesDeliveryIdRetryPost,
  rotateWebhookSecretV1ProjectsProjectIdWebhookEndpointsEndpointIdRotateSecretPost,
  sendWebhookTestV1ProjectsProjectIdWebhookEndpointsEndpointIdTestPost,
  updateWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdPatch,
} from "../../api/generated/endpoints/webhooks/webhooks";
import type {
  CreateWebhookEndpointRequest,
  DeliveryAttemptsOut,
  EventDetailOut,
  PageWebhookEndpointOut,
  UpdateWebhookEndpointRequest,
  WebhookEndpointOut,
  WebhookTestOut,
} from "../../api/generated/model";
import type { ApiError } from "../../lib/api";
import { LIST_LIMIT } from "../console/api";

export const ATTEMPTS_LIMIT = 50;
/** How often the attempts table refreshes on its own while the detail is open. */
export const ATTEMPTS_REFRESH_MS = 8_000;

const endpointsKey = (projectId: string): QueryKey => [
  getListWebhookEndpointsV1ProjectsProjectIdWebhookEndpointsGetUrl(projectId, { limit: LIST_LIMIT }),
];
const endpointKey = (projectId: string, endpointId: string): QueryKey => [
  getGetWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdGetUrl(projectId, endpointId),
];
const attemptsKey = (projectId: string, endpointId: string): QueryKey => [
  getListWebhookAttemptsV1ProjectsProjectIdWebhookEndpointsEndpointIdAttemptsGetUrl(projectId, endpointId, { limit: ATTEMPTS_LIMIT }),
];

export function useWebhookEndpoints(projectId: string | null, enabled = true) {
  return useQuery<PageWebhookEndpointOut, ApiError>({
    queryKey: projectId === null ? ["webhook-endpoints", "none"] : endpointsKey(projectId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listWebhookEndpointsV1ProjectsProjectIdWebhookEndpointsGet(projectId, { limit: LIST_LIMIT }, { signal });
    },
    enabled: projectId !== null && enabled,
  });
}

export function useWebhookEndpoint(projectId: string | null, endpointId: string) {
  return useQuery<WebhookEndpointOut, ApiError>({
    queryKey: projectId === null ? ["webhook-endpoint", "none"] : endpointKey(projectId, endpointId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return getWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdGet(projectId, endpointId, { signal });
    },
    enabled: projectId !== null,
  });
}

export function useWebhookAttempts(projectId: string | null, endpointId: string) {
  return useQuery<DeliveryAttemptsOut, ApiError>({
    queryKey: projectId === null ? ["webhook-attempts", "none"] : attemptsKey(projectId, endpointId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listWebhookAttemptsV1ProjectsProjectIdWebhookEndpointsEndpointIdAttemptsGet(projectId, endpointId, { limit: ATTEMPTS_LIMIT }, { signal });
    },
    enabled: projectId !== null,
    refetchInterval: ATTEMPTS_REFRESH_MS,
  });
}

export interface SecretVariables<T> {
  request: T;
  /** Receives the one-time `whsec_…` secret; the mutation itself resolves without it. */
  onSecret: (secret: string) => void;
}

export function useCreateWebhookEndpoint(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<WebhookEndpointOut, ApiError, SecretVariables<CreateWebhookEndpointRequest>>({
    mutationFn: async ({ request, onSecret }) => {
      const { secret, ...view } = await createWebhookEndpointV1ProjectsProjectIdWebhookEndpointsPost(projectId, request);
      onSecret(secret);
      return view;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: endpointsKey(projectId) }),
  });
}

export function useRotateWebhookSecret(projectId: string, endpointId: string) {
  const queryClient = useQueryClient();
  return useMutation<WebhookEndpointOut, ApiError, { onSecret: (secret: string) => void }>({
    mutationFn: async ({ onSecret }) => {
      const { secret, ...view } = await rotateWebhookSecretV1ProjectsProjectIdWebhookEndpointsEndpointIdRotateSecretPost(projectId, endpointId);
      onSecret(secret);
      return view;
    },
    onSuccess: (view) => {
      queryClient.setQueryData(endpointKey(projectId, endpointId), view);
      return queryClient.invalidateQueries({ queryKey: endpointsKey(projectId) });
    },
  });
}

function useEndpointChange<V>(projectId: string, endpointId: string, run: (variables: V) => Promise<WebhookEndpointOut>) {
  const queryClient = useQueryClient();
  return useMutation<WebhookEndpointOut, ApiError, V>({
    mutationFn: run,
    onSuccess: (view) => {
      queryClient.setQueryData(endpointKey(projectId, endpointId), view);
      return queryClient.invalidateQueries({ queryKey: endpointsKey(projectId) });
    },
  });
}

export function useUpdateWebhookEndpoint(projectId: string, endpointId: string) {
  return useEndpointChange<UpdateWebhookEndpointRequest>(projectId, endpointId, (request) =>
    updateWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdPatch(projectId, endpointId, request),
  );
}

export function useDisableWebhookEndpoint(projectId: string, endpointId: string) {
  return useEndpointChange<void>(projectId, endpointId, () =>
    disableWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdDisablePost(projectId, endpointId),
  );
}

/** Re-enabling is a PATCH `enabled: true` (disable has its own action). */
export function useEnableWebhookEndpoint(projectId: string, endpointId: string) {
  return useEndpointChange<void>(projectId, endpointId, () =>
    updateWebhookEndpointV1ProjectsProjectIdWebhookEndpointsEndpointIdPatch(projectId, endpointId, { enabled: true }),
  );
}

export function useSendWebhookTest(projectId: string, endpointId: string) {
  const queryClient = useQueryClient();
  return useMutation<WebhookTestOut, ApiError, void>({
    mutationFn: () => sendWebhookTestV1ProjectsProjectIdWebhookEndpointsEndpointIdTestPost(projectId, endpointId),
    // Not awaited: the button must not stay in "sending" while the attempts table refetches.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: attemptsKey(projectId, endpointId) });
    },
  });
}

/**
 * Manual redelivery of one delivery. The delivery shows as pending right away (optimistic) and rolls back
 * if the API refuses; the event detail is refetched either way to show the server's state.
 */
export function useRetryWebhookDelivery(projectId: string, eventId: string) {
  const queryClient = useQueryClient();
  const eventKey: QueryKey = [getGetProjectEventV1ProjectsProjectIdEventsEventIdGetUrl(projectId, eventId)];
  return useMutation<unknown, ApiError, string, { previous: EventDetailOut | undefined }>({
    mutationFn: (deliveryId) =>
      retryWebhookDeliveryV1ProjectsProjectIdEventsEventIdDeliveriesDeliveryIdRetryPost(projectId, eventId, deliveryId),
    onMutate: async (deliveryId) => {
      await queryClient.cancelQueries({ queryKey: eventKey });
      const previous = queryClient.getQueryData<EventDetailOut>(eventKey);
      if (previous) {
        queryClient.setQueryData<EventDetailOut>(eventKey, {
          ...previous,
          deliveries: previous.deliveries.map((delivery) => (delivery.id === deliveryId ? { ...delivery, status: "pending" } : delivery)),
        });
      }
      return { previous };
    },
    onError: (_error, _deliveryId, context) => {
      if (context?.previous) queryClient.setQueryData(eventKey, context.previous);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: eventKey }),
  });
}
