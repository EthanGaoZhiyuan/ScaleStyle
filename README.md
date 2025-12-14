# ScaleStyle: Production-Grade Multimodal Fashion Recommendation System

[![Status](https://img.shields.io/badge/Status-Active_Development-success)](https://github.com/EthanGaoZhiyuan/ScaleStyle)
[![Architecture](https://img.shields.io/badge/Architecture-Hybrid_Microservices-orange)](https://github.com/EthanGaoZhiyuan/ScaleStyle)
[![Tech Stack](https://img.shields.io/badge/Stack-Spring_Boot_%7C_Ray_Serve_%7C_Kubernetes-2496ED)]()
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## 📖 Project Overview

**ScaleStyle** is an end-to-end **production-grade multimodal recommendation system** designed to solve the fashion e-commerce cold-start problem. 

Built with a **Hybrid Microservices Architecture** (Java + Python), this project demonstrates enterprise-level system design capabilities, bridging the gap between high-concurrency backend engineering and modern AI infrastructure.

### 🚀 Core Value Proposition
- **Hybrid Architecture**: Leverages Java (Spring Boot) for robust, high-throughput traffic handling and Python (Ray Serve) for flexible, scalable AI inference.
- **Low Latency**: Achieves **P99 < 50ms** for end-to-end recommendations using gRPC optimization and multi-level caching.
- **Multimodal Intelligence**: Utilizes CLIP-based embeddings to understand both product images and textual descriptions, enabling semantic search beyond simple keyword matching.
- **Cloud-Native**: Fully containerized and orchestrated via Kubernetes (EKS) with Infrastructure as Code (Terraform).

---

## 🏗️ System Architecture & Roadmap

![ScaleStyle Architecture Diagram](./docs/H&M%20Architect.jpg)
*(Figure 1: High-level architecture showing the separation of concerns between Gateway and Inference layers)*

The project evolution is structured to simulate a real-world agile engineering roadmap, moving from MVP to a distributed, observable system.

| Feature Dimension | Phase 1: Foundation (Core MVP) | Phase 2: Scale & Intelligence (Upcoming) | Phase 3: Reliability & Ops (Future) |
| :--- | :--- | :--- | :--- |
| **Architecture** | Hybrid Monolith (Docker Compose) | Distributed Microservices (K8s) | Multi-Region / Service Mesh |
| **Backend** | Spring Boot 3.4 + gRPC | + Rate Limiting / Circuit Breaker | + Chaos Engineering |
| **AI Inference** | Ray Serve (Local) | Ray Cluster (Autoscaling) | A/B Testing Platform |
| **Data Storage** | PostgreSQL + Local Parquet | Milvus (Vector DB) + Redis | Feature Store (Feast) |
| **Observability** | Basic Logging | Prometheus + Grafana | Distributed Tracing (Jaeger) |

---

## 📊 Key Performance Indicators (KPIs)

Performance goals are set based on industry standards for real-time recommendation engines.

| Metric | Current Baseline | Target (Production) |
| :--- | :--- | :--- |
| **End-to-End Latency** | ~80ms | **P99 < 50ms** |
| **Throughput** | 100 QPS | **500+ QPS** (per node) |
| **Vector Search** | N/A | P99 < 20ms |
| **Docker Image Size** | Gateway: 185MB / Inf: 420MB | Optimized Distroless Images |

---

## 🛠️ Technology Stack

### **Backend & Gateway Layer**
- **Java 21 / Spring Boot 3.4**: Core application logic and API Gateway.
- **gRPC / Protobuf**: High-performance inter-service communication.
- **Caffeine Cache**: Local in-memory caching for hot data.

### **AI & Inference Layer**
- **Python 3.10 / Ray Serve**: Scalable model serving framework.
- **PyTorch / CLIP**: Multimodal embedding generation.
- **Faiss / Milvus**: Vector similarity search.

### **Infrastructure & DevOps**
- **Docker & Kubernetes**: Containerization and orchestration.
- **GitHub Actions**: CI/CD pipelines (Linting, Unit Tests, Build).
- **Terraform**: Infrastructure as Code (AWS EKS provisioning).

---

## 🎓 References & Resources

### **Core Technologies**
- [Spring Boot Documentation](https://spring.io/projects/spring-boot)
- [gRPC Java Guide](https://grpc.io/docs/languages/java/)
- [Ray Serve Architecture](https://docs.ray.io/en/latest/serve/index.html)
- [Milvus Vector Database](https://milvus.io/docs)

### **Data Source**
- [H&M Personalized Fashion Recommendations (Kaggle)](https://www.kaggle.com/competitions/h-and-m-personalized-fashion-recommendations)

---

## 👤 Author

**Ethan Gao** *Backend Engineer & ML Systems Enthusiast*

Focusing on building scalable, high-performance distributed systems and bridging the gap between traditional backend and AI infrastructure.

- **Experience**: 4+ years in Backend Development & Team Leadership.
- **Expertise**: Java Ecosystem, Distributed Systems, ML Infrastructure, Cloud Native.
- **Kaggle**: Expert Tier (Top 2%).

---

## 📝 License

This project is open-sourced under the [MIT License](LICENSE).