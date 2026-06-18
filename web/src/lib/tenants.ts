// 租户 id → 展示名（侧栏用）。后端 tenant_id 是 UUID；这里给已知 demo 租户友好名，
// 未知则回退显示 id。真实多租户接入后可改为从 /auth 或 /me 拉租户名。
export const TENANT_NAMES: Record<string, string> = {
  "11111111-1111-1111-1111-111111111111": "演示租户",
};
