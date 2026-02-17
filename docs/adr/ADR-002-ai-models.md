# ADR-002 — Motor de IA Híbrido: Gemini 3 Flash, Raciocínio Dinâmico e Estrutura Agnóstica

* **Status:** Accepted (Supersedes ADR-002 v1)
* **Data:** 2026-02-17
* **Decisores:** Architect / Márcio (Proprietário)
* **Impacto:** Backend (`hotelly-v2`), Infraestrutura (GCP)

## 1. Contexto e Problema
O Hotelly V2 utiliza IA para processamento de linguagem natural no canal WhatsApp. A versão anterior (ADR-001/002v1) baseava-se no Gemini 2.5 Flash.

**Fatores Críticos de Mudança:**
1.  **Obsolescência Programada:** O suporte ao Gemini 2.5 encerra-se em **junho de 2026**.
2.  **Evolução do Modelo:** O **Gemini 3 Flash** introduziu *thinking levels* controláveis, permitindo balancear custo (nível `minimal`) e raciocínio complexo (nível `high`).
3.  **Necessidade de Humanização:** A resposta instantânea da IA é percebida como robótica; o sistema requer latência artificial controlada.

## 2. Decisão Técnica

### 2.1. Modelo e Provedor Principal
* **Motor:** Adotar **Gemini 3 Flash** (`gemini-3-flash-preview`) como padrão.
* **Infraestrutura:** Utilizar via **Vertex AI** (Google Cloud) para garantir baixa latência interna e segurança corporativa.
* **SDK:** Uso obrigatório do SDK oficial Google Generative AI para suporte nativo a *Thought Signatures*.

### 2.2. Arquitetura Agnóstica (Interface Universal)
O backend não deve acoplar-se à implementação específica do Gemini. Deve ser criada uma interface (Wrapper) que suporte futuramente a injeção de outros modelos (ex: Claude 3.5 Sonnet).

**Contrato da Interface (Pseudocódigo):**
```typescript
interface AIProvider {
  processMessage(
    message: string,
    history: Message[],
    options: {
      thinkingLevel?: 'minimal' | 'low' | 'medium' | 'high'; // Default: via ENV
      provider?: 'google' | 'anthropic'; // Default: 'google'
      temperature?: number;
    }
  ): Promise<IntentOutput>;
}

```

### 2.3. Estratégia de Raciocínio Dinâmico (Thinking Levels)

O sistema deve decidir o nível de esforço cognitivo baseando-se na complexidade da tarefa ou no histórico de falha (retry).

| Cenário / Intenção | Nível Recomendado | Justificativa |
| --- | --- | --- |
| **Triagem Inicial (Router)** | `minimal` | Classificação rápida e barata. |
| **Saudações / FAQs** | `minimal` | Respostas estáticas, sem raciocínio lógico. |
| **Consultar Disponibilidade (Simples)** | `low` | Datas explícitas ("Dia 20 ao 22"). |
| **Alteração / Cancelamento** | `medium` | Envolve regras de negócio e cálculo de multas. |
| **Ambiguidade / Reclamação** | `high` | Entendimento de nuances, ironia ou datas relativas complexas. |
| **Retry Automático** | `high` | Se Confiança < 0.7 na tentativa anterior. |

### 2.4. Otimização de Custos (Context Caching)

* **Mandatório:** O *System Prompt*, *Regras de Negócio* e *Schema JSON* devem ser cacheados.
* **Política de TTL:** O cache deve ter TTL (Time-to-Live) ajustado para o ciclo de deploy (ex: 24h) ou invalidado manualmente via script de CI/CD ao atualizar o `system_prompt.md`.

## 3. Drivers (Por que esta decisão)

* **Sobrevivência do Projeto:** Elimina o risco de "apagão" da IA em junho/2026.
* **Economia de Escala:** O uso de níveis `minimal` para 80% das interações (triagem/FAQ) reduz drasticamente o consumo de tokens de *thinking*.
* **Resiliência:** O *fallback* para níveis mais altos de raciocínio protege o sistema contra erros de interpretação em cenários complexos.
* **UX Humanizada:** O tempo de processamento da IA é desacoplado do envio da mensagem (uso de filas/workers), permitindo um *delay* intencional para simular digitação humana.

## 4. Especificação de Dados (Inputs e Outputs)

### 4.1. Variáveis de Ambiente (Secret Manager)

* `AI_PROVIDER`: "google" (Default)
* `AI_MODEL_ID`: "gemini-3-flash-preview"
* `AI_DEFAULT_THINKING_LEVEL`: "minimal"
* `AI_HIGH_THINKING_LEVEL`: "high" (Para retries)

### 4.2. Output Padronizado (JSON)

O modelo **não deve** retornar texto livre, apenas JSON validado.

```json
{
  "intent": "ENUM",
  "slots": {
    "check_in": "ISO-8601 | null",
    "check_out": "ISO-8601 | null",
    "guests": "integer | null",
    "adults": "integer | null",
    "children": "integer | null",
    "special_requests": "string | null"
  },
  "metadata": {
    "confidence_score": 0.0 - 1.0,
    "thinking_level_used": "minimal | low | medium | high",
    "detected_language": "pt-BR"
  },
  "reply_suggestion": "Texto da resposta sugerida para o usuário (human-like)."
}

```

## 5. Plano de Implementação (Sprint 1.8)

1. **Infra:** Criar segredos e configurar acesso à Vertex AI.
2. **Backend:** Implementar `AIServiceWrapper` com suporte a troca de níveis.
3. **Lógica:** Implementar fluxo de *Retry Inteligente*:
* Se `confidence_score < 0.7` usando `minimal` -> Reenviar prompt imediato usando `high`.
4. **Testes:** Criar "Golden Dataset" com frases ambíguas para validar se o nível `high` resolve o que o `minimal` erra.
