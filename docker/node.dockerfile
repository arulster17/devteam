FROM node:20-alpine

WORKDIR /workspace

# Deps layer — cached separately from source so rebuilds after code changes are fast
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline

COPY . .

CMD sh -c 'if [ -n "$RESULTS_PATH" ]; then npx jest --no-coverage --forceExit --json --outputFile="$RESULTS_PATH"; else npx jest --no-coverage --forceExit; fi'
