// /api/logout — see lib/account.mjs
import { handle } from "../../lib/account.mjs";

export const onRequest = ctx => handle("logout", ctx);
