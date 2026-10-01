# Recorded fixtures — workspaces

Bodies as the API returns them, written from the platform's OpenAPI for workspaces (into
which the `workspace` field on `/users/me` and the invitations were folded on 8 Sep) and
checked against its DTOs: every required field present, no field
the schema does not have (`WorkspaceResponse`, `WorkspaceMemberResponse`, `WorkspaceInvitationResponse`,
`ActiveWorkspaceResponse`, `PodListItemResponse` with its required `executor: ExecutorForPodResponse` and a
`TemplateBaseResponse` template). `GET /users/me` and `POST /keys` have no
response model; their bodies follow `UserService.get_user_info` (`workspace` = `ActiveWorkspaceResponse`) and the
`ApiKey` row. Addresses are TEST-NET (`203.0.113.x`) and `example.com`.

- `users_me_off.json` — a server with `WORKSPACES_ENABLED` off: no `workspace` key.
- `users_me_research.json` / `users_me_personal.json` — the key acts in the team / in the personal workspace.
- `workspaces_key.json` — `GET /workspaces` with an API key: the one workspace it acts in.
- `workspaces_session.json` — the same with a session: every workspace of the account, with the role in each.
- `members.json`, `invitation.json`, `key_created.json`; `login.json` — `POST /users/login`, read for `token` only (its `user` block is the `/users/me` shape, not `UserResponseDto`).
- `pods_research.json` / `pods_off.json` — `GET /pods` with and without `workspace_id`.
