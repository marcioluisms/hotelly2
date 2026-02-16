# ADR-010 — Modelo de Dados para Receita Auxiliar (Extras)

**Status:** Proposed
**Data:** 2026-02-16
**Decisores:** Produto/Engenharia (Hotelly V2)

## Contexto

Hotéis monetizam além da diária através de serviços auxiliares (café da manhã, transfer, late checkout, frigobar, etc.). Esses itens possuem regras de precificação distintas — alguns são cobrados uma única vez, outros por noite, por hóspede ou pela combinação hóspede × noite. Precisamos de um modelo flexível que:

- suporte os quatro modos de precificação mais comuns da hotelaria,
- preserve o preço histórico no momento da venda (snapshot), evitando que alterações no catálogo afetem reservas já realizadas,
- siga os princípios inegociáveis do projeto: valores monetários em centavos, escopo por `property_id` e zero PII no schema.

## Decisão

Adotar um **catálogo de extras por property** (`extras`) e uma **tabela de venda/snapshot** (`reservation_extras`) que congela preço e modo de cobrança no momento da reserva.

---

## Enum `ExtraPricingMode`

Define como o preço unitário é multiplicado para compor o total.

| Valor                  | Descrição                                               | Fórmula do total                                  |
|------------------------|---------------------------------------------------------|---------------------------------------------------|
| `PER_UNIT`             | Preço fixo, cobrado uma única vez por unidade           | `unit_price × quantity`                           |
| `PER_NIGHT`            | Preço multiplicado pelo número de noites                | `unit_price × quantity × nights`                  |
| `PER_GUEST`            | Preço multiplicado pelo total de hóspedes               | `unit_price × quantity × total_guests`            |
| `PER_GUEST_PER_NIGHT`  | Preço multiplicado por hóspedes **e** noites            | `unit_price × quantity × total_guests × nights`   |

> **Definições:**
> - `nights` = `(check_out - check_in)` em dias.
> - `total_guests` = `adults + children` da reserva.

---

## Modelo de Dados

### Tabela `extras` (Catálogo)

Catálogo de extras disponíveis para venda, com escopo por property.

| Coluna               | Tipo           | Restrições                        |
|----------------------|----------------|-----------------------------------|
| `id`                 | UUID           | PK, default `gen_random_uuid()`   |
| `property_id`        | TEXT           | FK `properties(id)`, NOT NULL     |
| `name`               | TEXT           | NOT NULL                          |
| `description`        | TEXT           | nullable                          |
| `pricing_mode`       | TEXT (enum)    | NOT NULL, CHECK valores do enum   |
| `default_price_cents`| INTEGER        | NOT NULL, >= 0                    |
| `created_at`         | TIMESTAMPTZ    | NOT NULL, default `now()`         |
| `updated_at`         | TIMESTAMPTZ    | NOT NULL, default `now()`         |

- Índice em `property_id` para queries por property.

### Tabela `reservation_extras` (Venda / Snapshot)

Registro de extras vinculados a uma reserva. Congela preço e modo de cobrança no momento da venda (**Snapshot Rule**).

| Coluna                        | Tipo           | Restrições                        |
|-------------------------------|----------------|-----------------------------------|
| `id`                          | UUID           | PK, default `gen_random_uuid()`   |
| `reservation_id`              | UUID           | FK `reservations(id)`, NOT NULL   |
| `extra_id`                    | UUID           | FK `extras(id)`, NOT NULL         |
| `unit_price_cents_at_booking` | INTEGER        | NOT NULL, >= 0 (snapshot)         |
| `pricing_mode_at_booking`     | TEXT (enum)    | NOT NULL (snapshot)               |
| `quantity`                    | INTEGER        | NOT NULL, default 1, >= 1         |
| `total_price_cents`           | INTEGER        | NOT NULL, >= 0 (calculado)        |
| `created_at`                  | TIMESTAMPTZ    | NOT NULL, default `now()`         |

- Índice em `reservation_id` para listar extras de uma reserva.
- `extra_id` mantido como referência ao catálogo (navegação/relatórios), mas **nunca** usado para derivar preço — o preço vem exclusivamente dos campos `_at_booking`.

---

## DDL de Referência

```sql
-- Enum de modos de precificação (tipo nativo PostgreSQL)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'extra_pricing_mode') THEN
    CREATE TYPE extra_pricing_mode AS ENUM (
      'PER_UNIT', 'PER_NIGHT', 'PER_GUEST', 'PER_GUEST_PER_NIGHT'
    );
  END IF;
END $$;

-- Catálogo de extras por property
CREATE TABLE IF NOT EXISTS extras (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  property_id         TEXT NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  name                TEXT NOT NULL,
  description         TEXT,
  pricing_mode        extra_pricing_mode NOT NULL,
  default_price_cents INTEGER NOT NULL CHECK (default_price_cents >= 0),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extras_property_id ON extras(property_id);

-- Extras vendidos por reserva (snapshot de preço)
CREATE TABLE IF NOT EXISTS reservation_extras (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reservation_id              UUID NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
  extra_id                    UUID NOT NULL REFERENCES extras(id) ON DELETE RESTRICT,
  unit_price_cents_at_booking INTEGER NOT NULL CHECK (unit_price_cents_at_booking >= 0),
  pricing_mode_at_booking     extra_pricing_mode NOT NULL,
  quantity                    INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
  total_price_cents           INTEGER NOT NULL CHECK (total_price_cents >= 0),
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reservation_extras_reservation_id ON reservation_extras(reservation_id);
```

---

## Lógica de Negócio — Cálculo de `total_price_cents`

O campo `total_price_cents` é calculado no momento da criação do `reservation_extra` e persistido (não é coluna gerada). A lógica utiliza dados do snapshot e da reserva associada:

```
nights       = (reservation.check_out - reservation.check_in) em dias
total_guests = reservation.adults + reservation.children
unit_price   = unit_price_cents_at_booking
qty          = quantity
```

| `pricing_mode_at_booking` | Cálculo                                        |
|---------------------------|------------------------------------------------|
| `PER_UNIT`                | `unit_price × qty`                             |
| `PER_NIGHT`               | `unit_price × qty × nights`                    |
| `PER_GUEST`               | `unit_price × qty × total_guests`              |
| `PER_GUEST_PER_NIGHT`     | `unit_price × qty × total_guests × nights`     |

> **Regra:** o cálculo é executado no backend (camada de serviço/domínio) antes do INSERT. O valor persistido é a fonte de verdade para cobrança e relatórios.

---

## Snapshot Rule

Ao adicionar um extra a uma reserva:

1. Copiar `default_price_cents` → `unit_price_cents_at_booking`.
2. Copiar `pricing_mode` → `pricing_mode_at_booking`.
3. Calcular `total_price_cents` conforme a tabela acima.

Alterações futuras no catálogo (`extras`) **não** afetam registros existentes em `reservation_extras`. Isso garante integridade financeira e auditabilidade.

---

## Conformidade com Princípios do Projeto

| Princípio                      | Como é atendido                                                        |
|--------------------------------|------------------------------------------------------------------------|
| Valores monetários em centavos | `default_price_cents`, `unit_price_cents_at_booking`, `total_price_cents` — todos `INTEGER` em centavos |
| Escopo por property            | `extras.property_id` com índice; acesso sempre filtrado por property   |
| Zero PII no schema             | Nenhum campo contém dados pessoais de hóspedes                         |

## Consequências

- Flexibilidade para cobrir os cenários de precificação mais comuns da hotelaria.
- Imutabilidade financeira: o preço da venda nunca muda após o booking.
- Extensibilidade: novos modos de precificação podem ser adicionados ao enum sem alterar a estrutura das tabelas.
- `extra_id` como FK permite navegação e relatórios agregados por tipo de extra, sem comprometer a snapshot rule.
