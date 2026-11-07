# Radar de Concursos — PCI MCP + Telegram

Projeto inicial em Python para pesquisar oportunidades na PCI Concursos, filtrar palavras-chave, registrar resultados em SQLite e enviar alertas pelo Telegram.

## Limitações importantes da fonte

A página oficial da PCI MCP documenta cinco ferramentas, entre elas `buscar_por_cargo`, `buscar_por_cidade`, `listar_concursos` e `pesquisar_concursos`. As ferramentas de concursos descritas pela PCI filtram editais com inscrições abertas. Portanto, esta versão consulta resultados abertos; concursos apenas autorizados ou previstos **não são cobertos ainda** e exigirão uma segunda fonte oficial. A configuração registra esse interesse e o programa avisa sobre a limitação, sem fingir que monitorou tais anúncios.

A pesquisa por palavras-chave encontra candidatos, não prova elegibilidade. O programa rotula a formação como indício ou revisão necessária. A confirmação exige ler o edital oficial e verificar formação, habilitação, registro profissional quando aplicável e atribuições do cargo.

## Pré-requisitos

- **Python 3.11** ou superior.
- **Bot do Telegram:** Crie um bot conversando com o [@BotFather](https://t.me/botfather) para obter o `TELEGRAM_BOT_TOKEN`.
- **Chat ID:** Obtenha seu ID numérico (usando bots como o `@userinfobot`) para preencher o `TELEGRAM_CHAT_ID`.

## Instalação no Windows 11

1. Instale Python 3.11 ou superior.
2. Abra PowerShell nesta pasta.
3. Crie e ative o ambiente virtual:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. Copie `.env.example` para `.env` e preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Use o token novo se o anterior foi exposto. Nunca envie o `.env` a ninguém nem o versione.
5. Execute:

   ```powershell
   python main.py
   ```

O programa faz uma consulta ao iniciar e repete a cada 12 horas. Para interromper, use `Ctrl+C`. Para operar automaticamente, deixe o computador ligado ou configure o Agendador de Tarefas/um servidor.

## Personalização

Edite `config.json`:

- `frequencia_horas`: intervalo em horas.
- `localizacao.estados` e `localizacao.cidades`: listas vazias significam Brasil inteiro. O código atual só aplica um estado/cidade quando esses campos são preenchidos; para múltiplas regiões, será preciso expandir as chamadas.
- `areas_cargos`: palavras-chave para buscar e filtrar.
- `formacoes.curso_principal`: nomes da graduação-alvo.
- `formacoes.qualquer_graduacao`: procura expressões explícitas de aceitação de qualquer curso superior no texto retornado. Não substitui a leitura do edital.
- `situacoes_desejadas`: preferências de status. Nesta versão, a PCI MCP cobre editais abertos; `autorizado_ou_previsto` é apenas uma preferência pendente de uma fonte complementar.

## Diagnóstico

O log imprime as ferramentas MCP encontradas e cada consulta. Se o servidor mudar nomes ou parâmetros, o programa registra erros de ferramenta. Não considera uma consulta com erro como busca bem-sucedida.

## Fonte

Documentação oficial PCI MCP: https://www.pciconcursos.com.br/mcp-e-gpt
Servidor MCP: https://mcp.pciconcursos.com.br/mcp
