/*
 * Project members: thin query/mutation layer over the generated request functions. Keys start with the request
 * URL like the rest of the console layer, so realtime invalidation and `invalidateQueries` agree.
 */
import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";

import {
  addMemberV1ProjectsProjectIdMembersPost,
  changeMemberRoleV1ProjectsProjectIdMembersOperatorIdPatch,
  getListMembersV1ProjectsProjectIdMembersGetUrl,
  listMembersV1ProjectsProjectIdMembersGet,
  removeMemberV1ProjectsProjectIdMembersOperatorIdDelete,
} from "../../api/generated/endpoints/members/members";
import type { AddMemberRequest, MemberOut, MembersOut } from "../../api/generated/model";
import type { ApiError } from "../../lib/api";
import { meQueryOptions } from "../auth/auth-api";

const membersKey = (projectId: string): QueryKey => [getListMembersV1ProjectsProjectIdMembersGetUrl(projectId)];

export function useMembers(projectId: string | null) {
  return useQuery<MembersOut, ApiError>({
    queryKey: projectId === null ? ["members", "none"] : membersKey(projectId),
    queryFn: ({ signal }) => {
      if (projectId === null) throw new Error("no project");
      return listMembersV1ProjectsProjectIdMembersGet(projectId, { signal });
    },
    enabled: projectId !== null,
  });
}

/** Membership changes can alter the signed-in operator's own role, which `/auth/me` carries. */
function useRefresh(projectId: string) {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: membersKey(projectId) }),
      queryClient.invalidateQueries({ queryKey: meQueryOptions.queryKey }),
    ]);
}

export function useAddMember(projectId: string) {
  const refresh = useRefresh(projectId);
  return useMutation<MemberOut, ApiError, AddMemberRequest>({
    mutationFn: (request) => addMemberV1ProjectsProjectIdMembersPost(projectId, request),
    onSuccess: refresh,
  });
}

export function useChangeMemberRole(projectId: string) {
  const refresh = useRefresh(projectId);
  return useMutation<MemberOut, ApiError, { operatorId: string; role: string }>({
    mutationFn: ({ operatorId, role }) =>
      changeMemberRoleV1ProjectsProjectIdMembersOperatorIdPatch(projectId, operatorId, { role }),
    onSuccess: refresh,
  });
}

export function useRemoveMember(projectId: string) {
  const refresh = useRefresh(projectId);
  return useMutation<void, ApiError, string>({
    mutationFn: (operatorId) => removeMemberV1ProjectsProjectIdMembersOperatorIdDelete(projectId, operatorId),
    onSuccess: refresh,
  });
}
