@echo off
REM Script para gerenciar o projeto Olho de Aguia
REM Uso: run.bat [comando]

if "%1"=="" goto help
if "%1"=="start" goto start
if "%1"=="stop" goto stop
if "%1"=="restart" goto restart
if "%1"=="logs" goto logs
if "%1"=="test" goto test
if "%1"=="clean" goto clean
goto help

:start
echo 🚀 Iniciando Olho de Aguia...
docker-compose up --build -d
echo ✅ Aplicacao iniciada! Acesse http://localhost:5000
echo 💡 Use 'run.bat logs' para ver os logs
goto end

:stop
echo ⏹️ Parando Olho de Aguia...
docker-compose down
echo ✅ Aplicacao parada!
goto end

:restart
echo 🔄 Reiniciando Olho de Aguia...
docker-compose down
docker-compose up --build -d
echo ✅ Aplicacao reiniciada!
goto end

:logs
echo 📋 Mostrando logs...
docker-compose logs -f
goto end

:test
echo 🧪 Executando testes da API...
python test_api.py
goto end

:clean
echo 🧹 Limpando containers e volumes...
docker-compose down -v
docker system prune -f
echo ✅ Limpeza concluida!
goto end

:help
echo 📖 Olho de Aguia - Comandos disponiveis:
echo.
echo   start     - Inicia a aplicacao
echo   stop      - Para a aplicacao
echo   restart   - Reinicia a aplicacao
echo   logs      - Mostra logs em tempo real
echo   test      - Executa testes da API
echo   clean     - Limpa containers e volumes
echo.
echo Exemplos:
echo   run.bat start
echo   run.bat logs
echo   run.bat test
goto end

:end
