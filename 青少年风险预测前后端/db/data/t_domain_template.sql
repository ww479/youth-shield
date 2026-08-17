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
-- Dumping data for table `t_domain_template`
--

LOCK TABLES `t_domain_template` WRITE;
/*!40000 ALTER TABLE `t_domain_template` DISABLE KEYS */;
INSERT INTO `t_domain_template` (`id`, `domain_code`, `domain_id`, `domain_name`, `template_code`, `pattern`, `risk_action`) VALUES (1,'A','historical_cognition','历史认知','A-T1','[课本/教材/正史/官方] + 不会告诉你 + [真相/另一个版本/内幕]','解构权威来源'),(2,'A','historical_cognition','历史认知','A-T2','[历史/正史/教材] + 都是/只是 + [胜利者书写/宣传/编造/包装]','全盘削弱史实可信度'),(3,'A','historical_cognition','历史认知','A-T3','[英雄/烈士/先进典型] + 其实/不过/说到底 + [普通人/被包装/被迫/有私心/算计]','消解英雄崇高'),(4,'A','historical_cognition','历史认知','A-T4','从 [物理/生理/常识/逻辑] 看 + [英雄事迹/牺牲叙事] + 不可能/太假','用片面常识否定历史事实'),(5,'A','historical_cognition','历史认知','A-T5','[侵略/殖民/旧时代] + 也/至少 + 带来/留下 + [现代化/秩序/文明/建设]','淡化侵略伤害'),(6,'A','historical_cognition','历史认知','A-T6','如果当年/本可以 + [走另一条路/做另一种选择] + 今天会更好/完全不同','历史道路替代叙事'),(7,'A','historical_cognition','历史认知','A-T7','[辟谣/澄清/删除/限流] + 说明/反而证明 + [心虚/真相不能说]','阴谋反证闭环'),(8,'A','historical_cognition','历史认知','A-T8','[国旗/国歌/英烈/纪念日] + 适合/可以 + [鬼畜/玩梗/表情包/整活]','国家符号娱乐化'),(9,'B','institutional_identity','制度认同','B-T1','[这条路/这套制度/中国特色] + [走错了/没有前途/注定失败]','制度道路否定'),(10,'B','institutional_identity','制度认同','B-T2','[个案/地方问题/热点事件] + 说明/证明 + [整个系统/制度/国家] + 有问题','局部问题政治化扩大'),(11,'B','institutional_identity','制度认同','B-T3','看看 [国外/西方/别人] + 再看看 [我们/这里]','片面国际比较'),(12,'B','institutional_identity','制度认同','B-T4','[国外/西方/外媒] + 才是/才有 + [真相/文明/自由/尊严]','外部绝对美化'),(13,'B','institutional_identity','制度认同','B-T5','[这里/这个国家/这片土地] + [永远不会变好/没有未来/不值得]','国家整体否定'),(14,'B','institutional_identity','制度认同','B-T6','[爱国/集体/跟党走] + 就是/等于 + [洗脑/盲从/表演/低级]','政治与国家认同污名化'),(15,'B','institutional_identity','制度认同','B-T7','要 [高级/文明/现代] + 就要 + [去中国化/远离本土/切割传统]','文化主体性削弱'),(16,'B','institutional_identity','制度认同','B-T8','每个 [新闻/事件/现实] + 都在提醒我 + [快走/润/离开/切割]','身份疏离动员'),(17,'C','psychological_resilience','心理韧性','C-T1','[普通人/学生/寒门/年轻人] + 再怎么 [努力/学习/奋斗] + 也 [没用/翻不了身]','努力无用绝对化'),(18,'C','psychological_resilience','心理韧性','C-T2','[努力/奋斗/责任/理想] + 是/只是 + [骗局/枷锁/燃料/管理工具/驯化]','价值消解'),(19,'C','psychological_resilience','心理韧性','C-T3','[出身/阶层/资源] + 决定 + [命运/结局/人生]','命运封闭叙事'),(20,'C','psychological_resilience','心理韧性','C-T4','[看透/清醒/醒了] + 之后 + 只想 [躺平/摆烂/退场/不参与]','犬儒化身份认同'),(21,'C','psychological_resilience','心理韧性','C-T5','[公信力/社会/规则] + 已经/早就 + [崩塌/没人信/没救]','社会信任崩塌'),(22,'C','psychological_resilience','心理韧性','C-T6','[我/我们] + [消失/离开/结束] + 也不会有人在意','自伤风险暗示'),(23,'C','psychological_resilience','心理韧性','C-T7','[没有未来/反正都完了] + 所以/就 + [不怕后果/报复/毁掉]','极端行为风险'),(24,'C','psychological_resilience','心理韧性','C-T8','[评论区/弹幕] 大量出现 + [开摆/麻了/不想努力/人间不值得]','群体情绪感染'),(25,'D','network_literacy','网络素养','D-T1','[网上/评论区/小号] + [说说/口嗨/玩梗] + 而已','否认网络责任'),(26,'D','network_literacy','网络素养','D-T2','[开盒/挂人/人肉/社死] + 是/才是 + [正义/避雷/替天行道]','网暴正义化'),(27,'D','network_literacy','网络素养','D-T3','[真假不重要/没证据/听说] + 先 + [转发/扩散/冲]','谣言动员'),(28,'D','network_literacy','网络素养','D-T4','[规则/审核] + 可以/就是用来 + [钻空子/绕过/规避]','规则意识弱化'),(29,'D','network_literacy','网络素养','D-T5','用 [谐音/缩写/打码/小号/群聊] + 就 + [查不到/过审/没事]','规避治理'),(30,'D','network_literacy','网络素养','D-T6','[算法/平台/热搜] + 都在 + [控制/操控/洗脑]','信息环境阴谋化'),(31,'D','network_literacy','网络素养','D-T7','为了 [偶像/数据/热度] + [控评/集资/围攻/网暴] + 也值得','饭圈价值替代'),(32,'D','network_literacy','网络素养','D-T8','[未成年人/学生] + 也要/可以 + [集资/打赏/进私密群/为爱发电]','未成年人权益风险'),(33,'E','cognitive_closure','认知闭合','E-T1','不是 [我们/自己人] + 就是 + [敌人/帮凶/对立面]','敌我二分'),(34,'E','cognitive_closure','认知闭合','E-T2','[越辟谣/越解释/越删除] + 越说明 + [心虚/是真的/有内幕]','阴谋闭环'),(35,'E','cognitive_closure','认知闭合','E-T3','[懂的都懂/明白人自然明白/不能多说]','圈层暗示替代证据'),(36,'E','cognitive_closure','认知闭合','E-T4','只有 [清醒的人/独立思考的人] + 才 + [看懂/明白/觉醒]','认知身份分层'),(37,'E','cognitive_closure','认知闭合','E-T5','[三分钟/一句话] + 看懂/说清 + [复杂公共议题/历史/制度]','复杂议题过度简化'),(38,'E','cognitive_closure','认知闭合','E-T6','[某群体] + 都/天生/永远 + [负面标签]','群体污名化'),(39,'E','cognitive_closure','认知闭合','E-T7','[感觉/情绪/直觉] + 对了 + 就是真的/不用看证据','情绪替代事实'),(40,'E','cognitive_closure','认知闭合','E-T8','[所有信息/所有媒体/所有人] + 都 + [不能信/被操控/在骗你]','全面怀疑与认知关闭');
/*!40000 ALTER TABLE `t_domain_template` ENABLE KEYS */;
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
