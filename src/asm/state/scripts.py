"""Atomic Redis Lua. PRD 52 - portfolio risk must be checked and reserved in one step.

The race this closes: two followed traders buy the same token 40ms apart. Both pass a
read-only risk check, both size at the cap, portfolio ends up 2x over limit. Doing
check-and-reserve inside a single Lua call makes that impossible.
"""
from __future__ import annotations

# KEYS[1] risk state hash
# KEYS[2] token exposure hash
# KEYS[3] trader exposure hash
# KEYS[4] reservation hash
# KEYS[5] system state string
# ARGV: 1 mint, 2 wallet, 3 size_usd, 4 max_daily_loss_pct, 5 max_drawdown_pct,
#       6 max_deployed_pct, 7 max_open_positions, 8 max_pending, 9 max_token_pct,
#       10 max_trader_pct, 11 min_position_usd, 12 ttl_seconds, 13 reservation_id
# RETURN: {1, reservation_id, granted_usd} | {0, REASON_CODE, detail}
RESERVE = """
local function num(v, d)
  if v == false or v == nil or v == '' then return d end
  return tonumber(v) or d
end

if redis.call('GET', KEYS[6]) then
  return {0, 'KILL_SWITCH_ACTIVE', 'kill_latched'}
end
local sys = redis.call('GET', KEYS[5])
-- Fail CLOSED on an absent or 'stopped' state. A fresh install must not trade before
-- someone explicitly starts it, and that guarantee has to live here - at the only step
-- that authorises spending - not in the UI.
if sys == false or sys == nil or sys == '' or sys == 'stopped' then
  return {0, 'SYSTEM_NOT_STARTED', 'stopped'}
end
if sys ~= 'running' and sys ~= 'soft_paused' and sys ~= 'hard_paused' and sys ~= 'killed' then
  return {0, 'SYSTEM_PAUSED', 'invalid_system_state'}
end
if sys == 'killed' or sys == 'hard_paused' then
  return {0, 'KILL_SWITCH_ACTIVE', sys}
end
if sys == 'soft_paused' then
  return {0, 'SYSTEM_PAUSED', sys}
end

local size        = num(ARGV[3], 0)
local min_size    = num(ARGV[11], 0)
if size < min_size then
  return {0, 'POSITION_BELOW_MINIMUM', tostring(size)}
end

local st = redis.call('HMGET', KEYS[1],
  'equity', 'cash', 'deployed', 'realized_today', 'hwm', 'open_positions', 'pending')
local equity   = num(st[1], 0)
local cash     = num(st[2], 0)
local deployed = num(st[3], 0)
local realized = num(st[4], 0)
local hwm      = num(st[5], 0)
local open_n   = num(st[6], 0)
local pending  = num(st[7], 0)

if equity <= 0 then
  return {0, 'INSUFFICIENT_CAPITAL', '0'}
end

-- PRD 23: daily loss circuit breaker (realized_today is negative on a losing day)
local daily_loss_pct = 0
if equity > 0 then daily_loss_pct = (-realized) / equity * 100 end
if daily_loss_pct >= num(ARGV[4], 100) then
  return {0, 'DAILY_LOSS_LIMIT', string.format('%.4f', daily_loss_pct)}
end

-- PRD 24: drawdown circuit breaker
if hwm > 0 then
  local dd = (hwm - equity) / hwm * 100
  if dd >= num(ARGV[5], 100) then
    return {0, 'DRAWDOWN_LIMIT', string.format('%.4f', dd)}
  end
end

-- PRD 22: total deployed ceiling
local max_deployed = num(ARGV[6], 100) / 100 * equity
if deployed + size > max_deployed then
  return {0, 'PORTFOLIO_DEPLOYED_LIMIT', string.format('%.4f', deployed + size)}
end

-- free cash must actually cover it
if size > cash then
  return {0, 'INSUFFICIENT_CAPITAL', string.format('%.4f', cash)}
end

-- PRD 17.7 / 38: position count and pending concurrency
if open_n + pending >= num(ARGV[7], 1e9) then
  return {0, 'MAX_OPEN_POSITIONS', tostring(open_n)}
end
if pending >= num(ARGV[8], 1e9) then
  return {0, 'TOO_MANY_PENDING', tostring(pending)}
end

-- PRD 17.7: per-token concentration
local tok_now = num(redis.call('HGET', KEYS[2], ARGV[1]), 0)
local max_tok = num(ARGV[9], 100) / 100 * equity
if tok_now + size > max_tok then
  return {0, 'TOKEN_EXPOSURE_LIMIT', string.format('%.4f', tok_now + size)}
end

-- PRD 25: per-trader risk budget
local tr_now = num(redis.call('HGET', KEYS[3], ARGV[2]), 0)
local max_tr = num(ARGV[10], 100) / 100 * equity
if tr_now + size > max_tr then
  return {0, 'TRADER_EXPOSURE_LIMIT', string.format('%.4f', tr_now + size)}
end

-- all gates passed: commit the reservation atomically
redis.call('HINCRBYFLOAT', KEYS[1], 'deployed', size)
redis.call('HINCRBYFLOAT', KEYS[1], 'cash', -size)
redis.call('HINCRBY',      KEYS[1], 'pending', 1)
redis.call('HINCRBYFLOAT', KEYS[2], ARGV[1], size)
redis.call('HINCRBYFLOAT', KEYS[3], ARGV[2], size)
redis.call('HSET', KEYS[4],
  'mint', ARGV[1], 'wallet', ARGV[2], 'usd', tostring(size), 'state', 'held')
-- Held reservations must survive timeouts until explicitly reconciled.
-- Expiring only this hash would permanently leak cash/exposure and pending slots.

return {1, ARGV[13], tostring(size)}
"""

# Release an unused reservation (execution failed / rejected downstream).
# KEYS: 1 state, 2 token exposure, 3 trader exposure, 4 reservation
# RETURN: {1, usd} | {0, 'NOT_FOUND'}
RELEASE = """
local res = redis.call('HGETALL', KEYS[4])
if #res == 0 then return {0, 'NOT_FOUND'} end
local h = {}
for i = 1, #res, 2 do h[res[i]] = res[i + 1] end
if h['state'] ~= 'held' then return {0, 'NOT_HELD'} end

local usd = tonumber(h['usd']) or 0
redis.call('HINCRBYFLOAT', KEYS[1], 'deployed', -usd)
redis.call('HINCRBYFLOAT', KEYS[1], 'cash', usd)
redis.call('HINCRBY',      KEYS[1], 'pending', -1)
redis.call('HINCRBYFLOAT', KEYS[2], h['mint'], -usd)
redis.call('HINCRBYFLOAT', KEYS[3], h['wallet'], -usd)
redis.call('DEL', KEYS[4])
return {1, tostring(usd)}
"""

# Convert a held reservation into a real position. Exposure stays; pending drops,
# open_positions rises, and the true filled amount replaces the estimate.
# ARGV: 1 actual_usd
COMMIT = """
local res = redis.call('HGETALL', KEYS[4])
if #res == 0 then return {0, 'NOT_FOUND'} end
local h = {}
for i = 1, #res, 2 do h[res[i]] = res[i + 1] end
if h['state'] ~= 'held' then return {0, 'NOT_HELD'} end

local reserved = tonumber(h['usd']) or 0
local actual   = tonumber(ARGV[1]) or reserved
local delta    = actual - reserved

redis.call('HINCRBYFLOAT', KEYS[1], 'deployed', delta)
redis.call('HINCRBYFLOAT', KEYS[1], 'cash', -delta)
redis.call('HINCRBY',      KEYS[1], 'pending', -1)
redis.call('HINCRBY',      KEYS[1], 'open_positions', 1)
redis.call('HINCRBYFLOAT', KEYS[2], h['mint'], delta)
redis.call('HINCRBYFLOAT', KEYS[3], h['wallet'], delta)
redis.call('HSET', KEYS[4], 'state', 'committed', 'usd', tostring(actual))
redis.call('EXPIRE', KEYS[4], 86400)
return {1, tostring(actual)}
"""

# Exit: release exposure held by a closed/reduced position and book realized pnl.
# ARGV: 1 mint, 2 wallet, 3 cost_released_usd, 4 realized_pnl_usd, 5 proceeds_usd,
#       6 fully_closed (0|1)
RELEASE_POSITION = """
local cost     = tonumber(ARGV[3]) or 0
local pnl      = tonumber(ARGV[4]) or 0
local proceeds = tonumber(ARGV[5]) or 0

redis.call('HINCRBYFLOAT', KEYS[1], 'deployed', -cost)
redis.call('HINCRBYFLOAT', KEYS[1], 'cash', proceeds)
redis.call('HINCRBYFLOAT', KEYS[1], 'realized_today', pnl)
redis.call('HINCRBYFLOAT', KEYS[2], ARGV[1], -cost)
redis.call('HINCRBYFLOAT', KEYS[3], ARGV[2], -cost)
if ARGV[6] == '1' then
  redis.call('HINCRBY', KEYS[1], 'open_positions', -1)
end

-- clamp tiny negative float dust to zero
for _, k in ipairs({'deployed'}) do
  local v = tonumber(redis.call('HGET', KEYS[1], k)) or 0
  if v < 0.000001 and v > -1 then redis.call('HSET', KEYS[1], k, '0') end
end
local te = tonumber(redis.call('HGET', KEYS[2], ARGV[1])) or 0
if te < 0.000001 then redis.call('HDEL', KEYS[2], ARGV[1]) end
local tr = tonumber(redis.call('HGET', KEYS[3], ARGV[2])) or 0
if tr < 0.000001 then redis.call('HDEL', KEYS[3], ARGV[2]) end

return {1, tostring(pnl)}
"""

# Mark equity/hwm after a mark-to-market tick. ARGV: 1 unrealized, 2 sol_balance
MARK = """
local st = redis.call('HMGET', KEYS[1], 'cash', 'deployed', 'hwm')
local cash     = tonumber(st[1]) or 0
local deployed = tonumber(st[2]) or 0
local hwm      = tonumber(st[3]) or 0
local unreal   = tonumber(ARGV[1]) or 0
local equity   = cash + deployed + unreal
redis.call('HSET', KEYS[1], 'equity', tostring(equity), 'unrealized', tostring(unreal))
if ARGV[2] ~= '' then redis.call('HSET', KEYS[1], 'sol_balance', ARGV[2]) end
if equity > hwm then
  redis.call('HSET', KEYS[1], 'hwm', tostring(equity))
  hwm = equity
end
return {tostring(equity), tostring(hwm)}
"""
