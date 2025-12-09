# 1. Rebuild (AMD64) & Push
docker build --platform linux/amd64 -t karasus15/tesla-tracker:latest .
docker push karasus15/tesla-tracker:latest

# 2. Force Kubernetes to pull the new image
kubectl delete pod -l app=tesla-tracker -n tesla-app