package com.ruoyi.alarm.mapper;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmRobot;

/**
 * 机器人 数据层
 *
 * @author ruoyi
 */
public interface AlarmRobotMapper
{
    /**
     * 查询机器人列表
     *
     * @param robot 机器人信息
     * @return 机器人集合
     */
    public List<AlarmRobot> selectAlarmRobotList(AlarmRobot robot);

    /**
     * 新增机器人
     *
     * @param robot 机器人信息
     * @return 结果
     */
    public int insertAlarmRobot(AlarmRobot robot);

    /**
     * 修改机器人
     *
     * @param robot 机器人信息
     * @return 结果
     */
    public int updateAlarmRobot(AlarmRobot robot);

    /**
     * 删除机器人
     *
     * @param robotId 机器人ID
     * @return 结果
     */
    public int deleteAlarmRobotById(Long robotId);

    /**
     * 根据ID查询机器人
     *
     * @param robotId 机器人ID
     * @return 机器人信息
     */
    public AlarmRobot selectAlarmRobotById(Long robotId);
}
