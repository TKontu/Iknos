Containers
docker run IMAGE # Run container
docker run -it IMAGE sh # Run interactive shell
docker ps # List running containers
docker ps -a # List all containers
docker stop CONTAINER # Stop container
docker start CONTAINER # Start container
docker restart CONTAINER # Restart container
docker rm CONTAINER # Remove container
docker logs CONTAINER # Show logs
docker exec -it CONTAINER sh # Exec shell

Images
docker images # List images
docker pull IMAGE # Download image
docker build -t NAME . # Build from Dockerfile
docker rmi IMAGE # Remove image

Volumes & Networks
docker volume ls # List volumes
docker volume rm VOLUME # Remove volume
docker network ls # List networks
docker network rm NETWORK # Remove network

System
docker info # System info
docker stats # Live stats
docker system df # Disk usage
docker system prune -a # Clean unused

Compose
docker compose up # Start services
docker compose up --build # Rebuild & start
docker compose up --force-recreate # Force recreate
docker compose up -d --force-recreate # detached
docker compose down # Stop & remove
docker compose down -v # Remove volumes too
docker compose build # Build services
docker compose build --no-cache # Force rebuild
docker compose logs # View logs

# shortlist:

docker compose down -v # Remove volumes too
docker compose build # Build services
docker compose up -d --force-recreate # detached
docker compose logs worker
docker compose logs api
docker compose logs postgres
docker compose up -d --build --force-recreate
