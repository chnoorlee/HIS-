import { UserManager, WebStorageStateStore } from "oidc-client-ts";
import { api, tokenStore } from "./api";
import type { User } from "./types";
import { PUBLIC_DEMO } from "./mode";

export interface AuthConfig {
  environment: string;
  mode: "local" | "oidc";
  authority?: string;
  client_id?: string;
  scope?: string;
  redirect_uri?: string;
}
let manager: UserManager | undefined;
function oidc(config: AuthConfig) {
  if (!config.authority || !config.client_id) throw new Error("医院统一身份认证尚未配置，请联系管理员。");
  manager ??= new UserManager({
    authority: config.authority,
    client_id: config.client_id,
    redirect_uri: config.redirect_uri ?? `${location.origin}/auth/callback`,
    post_logout_redirect_uri: location.origin,
    response_type: "code",
    scope: config.scope ?? "openid profile",
    automaticSilentRenew: false,
    userStore: new WebStorageStateStore({ store: sessionStorage }),
    stateStore: new WebStorageStateStore({ store: sessionStorage }),
  });
  return manager;
}
export async function restoreIdentity(config: AuthConfig): Promise<User | null> {
  if (PUBLIC_DEMO) return (await import("./demo")).demoUser;
  if (config.mode === "oidc") {
    const client = oidc(config);
    const query = new URLSearchParams(location.search);
    let identity;
    if (query.has("state") && (query.has("code") || query.has("error"))) {
      try { identity = await client.signinRedirectCallback(); }
      finally { history.replaceState({}, document.title, "/"); }
    } else identity = await client.getUser();
    if (!identity || identity.expired || !identity.access_token) {
      tokenStore.clear();
      await client.removeUser();
      return null;
    }
    tokenStore.set(identity.access_token);
  }
  return tokenStore.get() ? api<User>("/auth/me") : null;
}
export const signIn = (config: AuthConfig) => oidc(config).signinRedirect();
export async function clearIdentity() {
  tokenStore.clear();
  await manager?.removeUser();
}
export async function signOut(config: AuthConfig | null) {
  tokenStore.clear();
  if (config?.mode === "oidc") {
    const client = oidc(config);
    const identity = await client.getUser();
    await client.removeUser();
    await client.signoutRedirect({ id_token_hint: identity?.id_token });
  }
}
