# Build the application
# Use the official Gradle image as the build stage
From gradle:8.5-jdk21 AS builder

# Set the working directory inside the container
WORKDIR /app

# 1. Copy configuration and dependency files
COPY build.gradle settings.gradle ./

# 2. Copy the source code
COPY src ./src

# 3. Build the jar file
RUN gradle bootJar -x test --no-daemon

# Run the application
# Use the official OpenJDK image as the runtime stage
FROM eclipse-temurin:21-jre-alpine

WORKDIR /app

# 4. Copy the jar file from the builder stage
COPY --from=builder /app/build/libs/*.jar app.jar

# 5. Expose the application port
EXPOSE 8080

# 6. Command to run the application
ENTRYPOINT ["java", "-jar", "app.jar"]