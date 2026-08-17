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
-- Dumping data for table `t_l2_taxonomy`
--

LOCK TABLES `t_l2_taxonomy` WRITE;
/*!40000 ALTER TABLE `t_l2_taxonomy` DISABLE KEYS */;
INSERT INTO `t_l2_taxonomy` (`l2_code`, `l2_name`, `l1_code`, `sort_order`) VALUES ('L2-1','L2-1自由至上与个体中心','L1-1',1),('L2-10','L2-10情绪与极端内容优先','L1-5',10),('L2-11','L2-11流量驱动反权威叙事','',11),('L2-12','L2-12欧美生活方式模板','L1-6',12),('L2-13','L2-13西式审美与语言','',13),('L2-14','L2-14反体制爽文模板','L1-7',14),('L2-15','L2-15阶层跃升的资本逻辑','',15),('L2-16','L2-16情绪→消费的自动链接','L1-8',16),('L2-17','L2-17身份由消费决定','',17),('L2-18','L2-18逃离组织/体制','L1-9',18),('L2-19','L2-19完全自我负责主义','',19),('L2-2','L2-2自我实现的生活叙事','',2),('L2-20','L2-20单向优越叙事','L1-10',20),('L2-21','L2-21去历史化、去语境化比较','',21),('L2-22','L2-22反官僚轻量化','L1-11',22),('L2-23','L2-23去中心化与反组织信号','',23),('L2-24','L2-24欧美审美模板','L1-12',24),('L2-25','L2-25节日文化替代','',25),('L2-26','L2-26本土文化价值贬低','L1-13',26),('L2-27','L2-27科技霸权绑定','',27),('L2-28','L2-28环保议题政治化','L1-14',28),('L2-29','L2-29性别议题极端化','',29),('L2-3','L2-3资本符号（财富/精英）','L1-2',3),('L2-30','L2-30社群认知茧房','L1-15',30),('L2-31','L2-31理论根基腐蚀','L1-16',31),('L2-32','L2-32叙事框架扭曲','',32),('L2-33','L2-33隐性对抗叙事','',33),('L2-34','L2-34社会信任侵蚀','',34),('L2-4','L2-4好莱坞叙事模板','',4),('L2-5','L2-5多巴胺式内容节奏','L1-3',5),('L2-6','L2-6消费缓解情绪','',6),('L2-7','L2-7都市焦虑/无意义感','',7),('L2-8','L2-8市场逻辑解释社会','L1-4',8),('L2-9','L2-9专业化包装的去政治化','',9);
/*!40000 ALTER TABLE `t_l2_taxonomy` ENABLE KEYS */;
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
