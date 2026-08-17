USE youth_ideology;
-- MySQL dump 10.13  Distrib 9.7.1, for macos26.4 (arm64)
--
-- Host: 127.0.0.1    Database: youth_ideology
-- ------------------------------------------------------
-- Server version	9.7.1

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Dumping data for table `t_l1_taxonomy`
--

LOCK TABLES `t_l1_taxonomy` WRITE;
/*!40000 ALTER TABLE `t_l1_taxonomy` DISABLE KEYS */;
INSERT INTO `t_l1_taxonomy` (`l1_code`, `l1_name`, `sort_order`) VALUES ('L1-1','L1-1价值软包装',1),('L1-10','L1-10国际比较偏置',10),('L1-11','L1-11去政治化包装的反权威叙事',11),('L1-12','L1-12流行文化中的符号替代',12),('L1-13','L1-13文化身份消解',13),('L1-14','L1-14社会议题意识形态化',14),('L1-15','L1-15圈层化意识形态强化',15),('L1-16','L1-16政治靶向攻击',16),('L1-2','L1-2符号垄断',2),('L1-3','L1-3情绪渗透',3),('L1-4','L1-4伪中立专家化叙事（Pseudo-NeutralExpertise）',4),('L1-5','L1-5平台算法偏置',5),('L1-6','L1-6生活方式殖民',6),('L1-7','L1-7娱乐化叙事中的隐含价值',7),('L1-8','L1-8消费主义情绪绑定',8),('L1-9','L1-9KOL/KOC个体中心逻辑',9);
/*!40000 ALTER TABLE `t_l1_taxonomy` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-07-23 16:27:06
