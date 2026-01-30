# ADR-007 — Auth/RBAC (Dashboard + API)

**Status:** Proposed  
**Data:** 2026-01-29  
**Decisores:** Produto/Engenharia (Hotelly V2)

## Contexto
O Hotelly vai expor um dashboard (web) e endpoints de API que precisam de:
- autenticação (login/sessão),
- autorização (RBAC) por **property** (multi-tenancy por `property_id`),
- auditoria básica de ações.

O core transacional (reservas/pagamentos/WhatsApp) já é multi-tenant por `property_id`, mas hoje não existe camada de usuários/papéis.

## Problema
- Implementar auth próprio cedo (senha/reset/2FA) aumenta esforço e risco.
- Terceirizar também RBAC cria lock-in e desloca o modelo de domínio para fora do core.

## Decisão
Adotar **provedor externo de identidade (OIDC)** para autenticação (Authn) e manter **RBAC no Postgres** (Authz), com escopo **direto por property** no MVP.

- **Authn (quem é o usuário):** OIDC (ex.: Clerk/Auth0/Supabase Auth). Backend valida JWT via JWKS.
- **Authz (o que pode fazer):** tabela interna `user_property_roles` com role por `property_id`.

Motivo: acelera o dashboard sem criar dívida no core e permite trocar provedor de login depois sem refatorar autorização.

## Escopo do MVP
- **Sem `organizations` no MVP.**
- RBAC é **direto**: `user_id` → `property_id` → `role`.
- Evolução futura para `organizations` é possível adicionando tabelas novas (sem quebrar contratos atuais).

## Modelo de dados (mínimo)

### `users`
Usuários internos vinculados ao provedor OIDC.
- `id` (uuid, PK)
- `external_subject` (text, unique, NOT NULL) — claim `sub` do OIDC
- `email` (text, nullable)
- `name` (text, nullable)
- `created_at` (timestamptz)
- `updated_at` (timestamptz)

### `user_property_roles`
RBAC direto por property.
- `id` (uuid, PK)
- `user_id` (FK users)
- `property_id` (FK properties)
- `role` (text) — `owner | manager | staff | viewer`
- `created_at` (timestamptz)
- UNIQUE(`user_id`, `property_id`)

### DDL de referência

```sql
-- Usuários (vinculados ao provedor OIDC)
CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_subject TEXT UNIQUE NOT NULL,  -- 'sub' do OIDC
  email TEXT,
  name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Acesso por property (RBAC direto)
CREATE TABLE IF NOT EXISTS user_property_roles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  property_id TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'staff', 'viewer')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(user_id, property_id)
);
```

## Regras de autorização (v0)

### Resolução do usuário
- Toda request autenticada resolve um `user` interno via `external_subject = sub`.

### Endpoints property-scoped
Todo endpoint que opera dados de uma property deve:
- exigir `property_id` explícito (path/query) **ou** property selecionada em contexto, e
- checar `user_property_roles` para (`user_id`, `property_id`).

### Matriz de permissões (guideline)
- `owner`: tudo (inclui configurações e “áreas sensíveis” quando existirem).
- `manager`: operação + configurações (exceto billing “sensível” quando existir).
- `staff`: operação (reservas, conversas, pagamentos).
- `viewer`: somente leitura.

> Observação: “billing sensível” ainda não existe; esta matriz define direção para quando existir.

## Implementação (backend)
- Middleware/dependency FastAPI:
  1) extrai Bearer token,
  2) valida assinatura e claims (iss/aud/exp) via JWKS,
  3) lê `sub`,
  4) resolve `users` e carrega roles por property (cache/lookup),
  5) aplica 401/403 conforme o caso.
- Padronizar erro:
  - 401 se token inválido/ausente
  - 403 se autenticado sem permissão na property

## Alternativas consideradas

### A) Auth próprio (JWT + senha)
**Prós:** controle total.  
**Contras:** alto esforço, risco, distração.

### B) Tudo via serviço (Auth + RBAC)
**Prós:** rápido.  
**Contras:** lock-in forte e modelo de domínio fora do core.

**Escolha:** OIDC externo para Authn + RBAC interno (por property no MVP).

## Consequências
- Dashboard e API podem nascer rápido.
- RBAC fica alinhado ao domínio (`property_id`), sem lock-in.
- Trocar provedor de login não muda a base de autorização.

## Segurança / PII
- Email/nome são PII leve; armazenar minimamente e seguir política de retenção.
- Não armazenar tokens do provedor OIDC no banco.
- Auditoria deve registrar `user_id`, `property_id`, `action`, `correlation_id` (sem PII).

## Critérios de aceite
- `GET /me` retorna usuário e escopos (lista de properties + role).
- Usuário sem role na property não acessa endpoints property-scoped.
- Testes cobrindo 401/403 + pelo menos 1 endpoint property-scoped.
