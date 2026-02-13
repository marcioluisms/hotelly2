# ADR 008: Integridade de Reserva e Prevenção de Conflitos de Quarto

## Status
Proposto

## Contexto
É inaceitável para a operação hoteleira que o mesmo quarto físico () seja atribuído a duas reservas distintas no mesmo período. Precisamos de um algoritmo padronizado e centralizado para validar colisões.

## Decisão
Utilizaremos o algoritmo de interseção de intervalos para identificar conflitos:
Um conflito existe se: `(NovoInicio < FimExistente) AND (NovoFim > InicioExistente)`.

## Regras de Negócio
1. **Exclusividade de Check-out**: O check-out é considerado um momento de saída (manhã/meio-dia) e o novo check-in de entrada (tarde). Portanto, a comparação é estrita (`<` e `>`), permitindo que uma reserva comece no mesmo dia em que outra termina.
2. **Status Operacional**: Apenas reservas com status `confirmed`, `in_house` ou `checked_out` geram conflito. Reservas canceladas são ignoradas.
3. **Ignorar Auto-Conflito**: Ao editar datas de uma reserva já existente, o sistema deve ignorar o próprio ID da reserva para evitar falsos positivos.

## Consequências
- Garantia de integridade física dos quartos.
- Centralização da lógica de colisão no Core do domínio.
- Conformidade com a ADR-006 (PII), proibindo o log de dados de hóspedes em caso de erro de colisão.
