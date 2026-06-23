-- Kemy x Pokémon — bridge de MEMÓRIA pro mGBA (Pokémon Fire Red US).
-- Carregue no mGBA: Tools → Scripting → Load script → este arquivo.
-- Ele lê a memória do jogo e grava um JSON em TEMP a cada ~1s. A Kemy lê esse
-- arquivo pra jogar com estratégia (sabe HP, nível, posição) e aperta as teclas.
--
-- ⚠️ Os endereços são da versão Pokémon Fire Red (US/1.0). Se sua ROM for outra
-- (Leaf Green, PT-BR, 1.1), ajuste os endereços abaixo (datacrystal tem a lista).

local PARTY_COUNT = 0x02024029     -- quantidade de pokémon no time
local PARTY_SLOT0 = 0x02024284     -- início do 1º pokémon (struct de 100 bytes)
local OFF_LEVEL   = 0x54           -- offset do nível (dentro do struct)
local OFF_HP      = 0x56           -- HP atual (u16)
local OFF_MAXHP   = 0x58           -- HP máximo (u16)

local function tmpfile()
  local t = os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
  return t .. "/kemy_pokemon.json"
end

local function safe16(addr)
  local ok, v = pcall(function() return emu:read16(addr) end)
  if ok and v then return v else return 0 end
end
local function safe8(addr)
  local ok, v = pcall(function() return emu:read8(addr) end)
  if ok and v then return v else return 0 end
end

local frame = 0
local function onFrame()
  frame = frame + 1
  if frame % 60 ~= 0 then return end   -- ~1x por segundo (60fps)
  local count = safe8(PARTY_COUNT)
  if count > 6 then count = 0 end
  local lead = PARTY_SLOT0
  local lvl  = safe8(lead + OFF_LEVEL)
  local hp   = safe16(lead + OFF_HP)
  local maxhp = safe16(lead + OFF_MAXHP)
  local json = string.format(
    '{"party":%d,"lead":{"level":%d,"hp":%d,"maxhp":%d},"frame":%d}',
    count, lvl, hp, maxhp, frame)
  local f = io.open(tmpfile(), "w")
  if f then f:write(json); f:close() end
end

if callbacks then
  callbacks:add("frame", onFrame)
  if console then console:log("Kemy Pokémon bridge ativo — gravando estado em " .. tmpfile()) end
end
